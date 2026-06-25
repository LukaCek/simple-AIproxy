import os
import base64
import hashlib
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode, urlparse, urlunparse

import httpx
import json
import secrets
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yml"
DB_PATH = BASE_DIR / "app.db"

app = FastAPI(title="LLM API Gateway")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
config_lock = threading.RLock()
config_data: Dict[str, Any] = {}
http_client: Optional[httpx.AsyncClient] = None
watchdog_observer: Optional[Observer] = None
oauth_state_store: Dict[str, Dict[str, Any]] = {}
route_counters: Dict[str, int] = {}
route_counters_lock = threading.Lock()
playground_jobs: Dict[str, Dict[str, Any]] = {}
playground_jobs_lock = threading.Lock()
PROVIDER_TEST_TIMEOUT_SECONDS = 3600.0
UPSTREAM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("AIPROXY_UPSTREAM_TIMEOUT_SECONDS", "3600"))
MIN_COMPLETION_TOKENS = int(os.getenv("AIPROXY_MIN_COMPLETION_TOKENS", "1024"))
OLLAMA_DISABLE_THINKING = os.getenv("AIPROXY_OLLAMA_DISABLE_THINKING", "false").lower() not in {"0", "false", "no"}

admin_security = HTTPBasic()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")


def verify_admin(credentials: HTTPBasicCredentials = Depends(admin_security)) -> None:
    is_valid_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    is_valid_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (is_valid_username and is_valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS API_Keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS Logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_key TEXT,
                api_key_name TEXT,
                requested_model TEXT,
                group_name TEXT,
                provider_name TEXT,
                provider_model TEXT,
                status_code INTEGER,
                started_at TEXT,
                first_response_at TEXT,
                ended_at TEXT,
                first_response_ms REAL,
                total_ms REAL,
                prompt TEXT,
                output TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cursor.execute("PRAGMA table_info(Logs)")
        existing_columns = {row[1] for row in cursor.fetchall()}
        migrations = {
            "api_key_name": "TEXT",
            "requested_model": "TEXT",
            "first_response_at": "TEXT",
            "first_response_ms": "REAL",
            "total_ms": "REAL",
            "prompt": "TEXT",
            "output": "TEXT",
        }
        for column, column_type in migrations.items():
            if column not in existing_columns:
                cursor.execute(f"ALTER TABLE Logs ADD COLUMN {column} {column_type}")
        conn.commit()


def load_config() -> Dict[str, Any]:
    with config_lock:
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(f"Missing config file: {CONFIG_PATH}")
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return normalize_config_schema(data)


def save_config(data: Dict[str, Any]) -> None:
    with config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
        global config_data
        config_data = data


async def discover_models(base_url: str, api_key: str) -> List[str]:
    """Attempt to discover available model ids from a provider's models endpoint.

    Tries a few common candidate URLs and parses several response shapes.
    Returns a list of model id strings (may be empty on failure).
    """
    if not base_url:
        return []
    candidates: List[str] = []
    base = base_url.rstrip("/")
    candidates.append(base + "/v1/models")
    candidates.append(base + "/models")
    try:
        parsed = urlparse(base_url)
        root = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        candidates.append(root.rstrip("/") + "/v1/models")
        candidates.append(root.rstrip("/") + "/models")
        candidates.append(root.rstrip("/") + "/api/tags")
    except Exception:
        pass

    headers = {"User-Agent": "llm-proxy/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    found: List[str] = []
    for url in candidates:
        try:
            resp = await http_client.get(url, headers=headers, timeout=10.0)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        try:
            data = resp.json()
        except Exception:
            # not JSON
            continue

        # data could be: {"data": [{"id": "..."}, ...]}, {"models": [...]}, or a flat list
        candidates_list: List[str] = []
        if isinstance(data, dict):
            if isinstance(data.get("data"), list):
                for item in data.get("data", []):
                    if isinstance(item, dict):
                        v = item.get("id") or item.get("name")
                        if v:
                            candidates_list.append(str(v))
                    elif isinstance(item, str):
                        candidates_list.append(item)
            if not candidates_list and isinstance(data.get("models"), list):
                for item in data.get("models", []):
                    if isinstance(item, dict):
                        v = item.get("id") or item.get("name")
                        if v:
                            candidates_list.append(str(v))
                    elif isinstance(item, str):
                        candidates_list.append(item)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    v = item.get("id") or item.get("name")
                    if v:
                        candidates_list.append(str(v))
                elif isinstance(item, str):
                    candidates_list.append(item)

        # dedupe while preserving order
        for m in candidates_list:
            if m and m not in found:
                found.append(m)

        if found:
            break

    return found


def normalize_config_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {"providers": [], "groups": {}}

    if "providers" not in data:
        providers: List[Dict[str, Any]] = []
        provider_names: Dict[str, Dict[str, Any]] = {}
        groups = data.get("groups", {})
        new_groups: Dict[str, Any] = {}
        if isinstance(groups, dict):
            for group_name, group in groups.items():
                if not isinstance(group, dict):
                    continue
                members: List[Dict[str, Any]] = []
                for endpoint in group.get("endpoints", []):
                    if not isinstance(endpoint, dict):
                        continue
                    provider_name = str(endpoint.get("name", "")).strip()
                    if not provider_name:
                        continue
                    model_name = endpoint.get("model") or provider_name
                    if provider_name not in provider_names:
                        provider = {
                            "name": provider_name,
                            "url": endpoint.get("url", ""),
                            "api_key": endpoint.get("api_key", ""),
                            "description": endpoint.get("description", ""),
                            "models": [model_name] if model_name else [],
                        }
                        provider_names[provider_name] = provider
                        providers.append(provider)
                    else:
                        provider = provider_names[provider_name]
                        if model_name and model_name not in provider.get("models", []):
                            provider["models"].append(model_name)
                    members.append({"provider": provider_name, "model": model_name})
                new_groups[group_name] = {
                    "description": group.get("description", ""),
                    "members": members,
                }
        data["providers"] = providers
        data["groups"] = new_groups

    if isinstance(data.get("providers"), list):
        normalized_providers: List[Dict[str, Any]] = []
        for provider in data.get("providers", []):
            if not isinstance(provider, dict):
                continue
            models = provider.get("models")
            if models is None:
                maybe_model = provider.get("model")
                provider["models"] = [maybe_model] if maybe_model else []
            elif not isinstance(models, list):
                provider["models"] = [models]
            provider["models"] = [str(item) for item in provider.get("models", []) if item is not None and str(item).strip()]
            provider.setdefault("api_mode", "openai_chat_completions")
            normalized_providers.append(provider)
        data["providers"] = normalized_providers

    groups = data.get("groups", {})
    if not isinstance(groups, dict):
        data["groups"] = {}
        groups = data["groups"]
    if isinstance(groups, dict):
        for group in groups.values():
            if not isinstance(group, dict):
                continue
            if "members" not in group and "endpoints" in group:
                members: List[Dict[str, Any]] = []
                for endpoint in group.get("endpoints", []):
                    if not isinstance(endpoint, dict):
                        continue
                    members.append({
                        "provider": endpoint.get("name"),
                        "model": endpoint.get("model") or endpoint.get("name"),
                    })
                group["members"] = members
                group.pop("endpoints", None)
    return data


def reload_config() -> None:
    global config_data
    try:
        new_data = load_config()
        with config_lock:
            config_data = new_data
        print("config.yml reloaded from disk")
    except Exception as exc:
        print(f"Failed to reload config: {exc}")


class ConfigFileHandler(FileSystemEventHandler):
    def on_modified(self, event: Any) -> None:
        if not event.src_path.endswith("config.yml"):
            return
        reload_config()


@app.on_event("startup")
async def startup() -> None:
    global http_client, watchdog_observer, config_data
    init_database()
    config_data = load_config()
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(UPSTREAM_REQUEST_TIMEOUT_SECONDS, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )
    watchdog_observer = Observer()
    handler = ConfigFileHandler()
    watchdog_observer.schedule(handler, str(CONFIG_PATH.parent), recursive=False)
    watchdog_observer.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    global http_client, watchdog_observer
    if http_client is not None:
        await http_client.aclose()
    if watchdog_observer is not None:
        watchdog_observer.stop()
        watchdog_observer.join()


def get_api_key_record(api_key: str) -> Optional[sqlite3.Row]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM API_Keys WHERE key = ?", (api_key.strip(),))
        return cursor.fetchone()


async def validate_api_key(request: Request) -> sqlite3.Row:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split("Bearer ", 1)[1].strip()
    record = get_api_key_record(token)
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key", headers={"WWW-Authenticate": "Bearer"})
    return record


def list_api_keys() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, key, created_at FROM API_Keys ORDER BY created_at DESC")
        return [dict(row) for row in cursor.fetchall()]


def truncate_text(value: Any, limit: int = 12000) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def format_provider_exception(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return f"{exc.__class__.__name__}: {message}"
    return exc.__class__.__name__


def is_ollama_endpoint(endpoint: Dict[str, Any]) -> bool:
    marker = " ".join(str(endpoint.get(key, "")) for key in ("name", "url", "api_mode", "type", "provider_type"))
    return "ollama" in marker.lower()


def clamp_completion_token_limit(payload: Dict[str, Any]) -> None:
    if MIN_COMPLETION_TOKENS <= 0:
        return
    for field in ("max_tokens", "max_completion_tokens"):
        value = payload.get(field)
        if value is None:
            continue
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < numeric < MIN_COMPLETION_TOKENS:
            payload[field] = MIN_COMPLETION_TOKENS


def prepare_provider_chat_payload(payload: Dict[str, Any], endpoint: Dict[str, Any], provider_model: str) -> Dict[str, Any]:
    provider_payload = dict(payload)
    provider_payload["model"] = provider_model
    clamp_completion_token_limit(provider_payload)
    # Ollama thinking/reasoning models can spend a small Home Assistant token
    # budget entirely in `message.reasoning`, then return empty content with
    # finish_reason=length. Keep thinking enabled by default and solve the HA
    # issue by raising tiny token limits; operators can still opt out with
    # AIPROXY_OLLAMA_DISABLE_THINKING=true.
    if OLLAMA_DISABLE_THINKING and is_ollama_endpoint(endpoint) and "think" not in provider_payload:
        provider_payload["think"] = False
    return provider_payload


def extract_prompt(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""
    if payload.get("messages") is not None:
        return truncate_text(payload.get("messages"))
    return truncate_text(payload.get("prompt", ""))


def extract_output_from_chat_payload(data: Any) -> str:
    if not isinstance(data, dict):
        return truncate_text(data)
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        parts: List[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            content = message.get("content") or delta.get("content")
            if content:
                parts.append(str(content))
        if parts:
            return truncate_text("".join(parts))
    return truncate_text(data)


def extract_output_from_body(content: bytes, content_type: str = "") -> str:
    text = content.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or "").lower():
        parts: List[str] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data_text = line[5:].strip()
            if not data_text or data_text == "[DONE]":
                continue
            try:
                parts.append(extract_output_from_chat_payload(json.loads(data_text)))
            except Exception:
                parts.append(data_text)
        return truncate_text("".join(parts) or text)
    try:
        return extract_output_from_chat_payload(json.loads(text))
    except Exception:
        return truncate_text(text)


def list_logs(limit: int = 200) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, api_key, api_key_name, requested_model, group_name, provider_name, provider_model,
                   status_code, started_at, first_response_at, ended_at, first_response_ms, total_ms,
                   prompt, output, error, created_at
            FROM Logs ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        if row.get("total_ms") is not None:
            row["duration"] = f"{float(row['total_ms']) / 1000:.2f}s"
        else:
            try:
                start_dt = datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
                end_dt = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
                row["duration"] = f"{(end_dt - start_dt).total_seconds():.2f}s" if start_dt and end_dt else "-"
            except Exception:
                row["duration"] = "-"
        row["first_response"] = f"{float(row['first_response_ms']) / 1000:.2f}s" if row.get("first_response_ms") is not None else "-"
        key = row.get("api_key") or ""
        row["api_key_display"] = f"{row.get('api_key_name') or '-'} ({key[:6]}…{key[-4:]})" if key else (row.get("api_key_name") or "-")
        row["api_key_hash"] = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16] if key else "-"
        row["type"] = "LLM"
        row["request_id"] = f"log-{row.get('id')}"
        row["modal_payload"] = {k: v for k, v in row.items() if k not in {"api_key", "modal_payload"}}
    return rows


def insert_log(
    api_key: Optional[str],
    api_key_name: Optional[str],
    requested_model: str,
    group_name: str,
    provider_name: str,
    provider_model: str,
    status_code: int,
    started_at: str,
    first_response_at: Optional[str],
    ended_at: str,
    first_response_ms: Optional[float],
    total_ms: Optional[float],
    prompt: Optional[str],
    output: Optional[str],
    error: Optional[str],
) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO Logs (
                api_key, api_key_name, requested_model, group_name, provider_name, provider_model,
                status_code, started_at, first_response_at, ended_at, first_response_ms, total_ms,
                prompt, output, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                api_key,
                api_key_name,
                requested_model,
                group_name,
                provider_name,
                provider_model,
                status_code,
                started_at,
                first_response_at,
                ended_at,
                first_response_ms,
                total_ms,
                truncate_text(prompt or ""),
                truncate_text(output or ""),
                truncate_text(error or "", 4000) if error else None,
                datetime.utcnow().isoformat(),
            ),
        )
        conn.commit()

def create_api_key(name: str) -> str:
    token = uuid.uuid4().hex
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)", (name.strip(), token, datetime.utcnow().isoformat()))
        conn.commit()
    return token


def oauth_provider_connected(provider: Dict[str, Any]) -> bool:
    """Return whether an OAuth provider has usable server-side credentials.

    A provider counts as connected when it has a non-expired access token/api_key,
    or when it has a refresh token that can mint a new access token on demand.
    """
    access_token = str(provider.get("access_token") or provider.get("api_key") or "").strip()
    refresh_token = str(provider.get("refresh_token") or "").strip()
    if refresh_token:
        return True
    if not access_token:
        return False
    expires_at = parse_expires_at(provider.get("expires_at"))
    if expires_at is not None and expires_at <= time.time():
        return False
    return True


def get_providers() -> List[Dict[str, Any]]:
    providers_list: List[Dict[str, Any]] = []
    with config_lock:
        for provider in config_data.get("providers", []):
            if not isinstance(provider, dict):
                continue
            oauth_connected = oauth_provider_connected(provider) if provider.get("oauth") else False
            api_mode = provider.get("api_mode", "openai_chat_completions")
            providers_list.append(
                {
                    "name": provider.get("name", ""),
                    "url": provider.get("url", ""),
                    "api_key": provider.get("api_key", ""),
                    "description": provider.get("description", ""),
                    "models": provider.get("models", []),
                    "oauth": bool(provider.get("oauth")),
                    "oauth_connected": oauth_connected,
                    "oauth_needs_reauth": bool(provider.get("oauth")) and not oauth_connected,
                    "authorize_url": provider.get("authorize_url", ""),
                    "token_url": provider.get("token_url", ""),
                    "redirect_uri": provider.get("redirect_uri", ""),
                    "access_token": provider.get("access_token", ""),
                    "api_mode": api_mode,
                    "is_codex_oauth": bool(provider.get("oauth")) and api_mode == "codex_responses",
                    "expires_at": provider.get("expires_at", ""),
                }
            )
    return providers_list


def get_default_codex_provider() -> Dict[str, Any]:
    """Template for OpenAI Codex/ChatGPT OAuth tokens.

    Codex is not a normal /v1/chat/completions backend. The proxy exposes a
    chat-completions facade, but forwards Codex profiles through a Responses API
    adapter against chatgpt.com/backend-api/codex. Tokens can be pasted/imported
    into config.yml, or refreshed when refresh_token is present.
    """
    return {
        "name": "codex",
        "url": "https://chatgpt.com/backend-api/codex",
        "description": "OpenAI Codex OAuth profile (Responses adapter)",
        "models": ["gpt-5.5"],
        "api_mode": "codex_responses",
        "oauth": True,
        "client_id": os.getenv("CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"),
        "client_secret": os.getenv("CODEX_CLIENT_SECRET", ""),
        "authorize_url": "",
        "token_url": "https://auth.openai.com/oauth/token",
        "redirect_uri": "",
        "scopes": [],
    }


def find_provider(name: str) -> Optional[Dict[str, Any]]:
    with config_lock:
        for provider in config_data.get("providers", []):
            if not isinstance(provider, dict):
                continue
            if provider.get("name") == name:
                return provider
    return None


def get_models_for_provider(provider_name: str) -> List[str]:
    provider = find_provider(provider_name)
    if provider is None:
        return []
    models = provider.get("models", [])
    return [str(model) for model in models if model is not None]


def get_oauth_callback_url(request: Request, provider_name: str, provider: Dict[str, Any]) -> str:
    redirect_uri = provider.get("redirect_uri")
    if redirect_uri:
        return str(redirect_uri)
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    return base_url + f"/admin/providers/{provider_name}/oauth/callback"


def get_groups() -> List[Dict[str, Any]]:
    groups_list: List[Dict[str, Any]] = []
    with config_lock:
        groups = config_data.get("groups", {})
        for group_name, group in groups.items():
            if not isinstance(group, dict):
                continue
            members = group.get("members", [])
            groups_list.append(
                {
                    "name": group_name,
                    "description": group.get("description", ""),
                    "strategy": group.get("strategy", "round_robin"),
                    "members": members if isinstance(members, list) else [],
                }
            )
    return groups_list


def normalize_group_members(
    member_providers: List[str],
    member_models: List[str],
    member_types: Optional[List[str]] = None,
    member_groups: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    members: List[Dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    max_len = max(len(member_providers), len(member_models), len(member_types or []), len(member_groups or []))
    for index in range(max_len):
        member_type = (member_types[index] if member_types and index < len(member_types) else "provider").strip() or "provider"
        if member_type == "group":
            group_name = (member_groups[index] if member_groups and index < len(member_groups) else "").strip()
            if not group_name:
                continue
            key = ("group", group_name, "")
            if key in seen:
                continue
            seen.add(key)
            members.append({"group": group_name})
            continue
        provider_name = (member_providers[index] if index < len(member_providers) else "").strip()
        model_name = (member_models[index] if index < len(member_models) else "").strip()
        if not provider_name or not model_name:
            continue
        key = ("provider", provider_name, model_name)
        if key in seen:
            continue
        seen.add(key)
        members.append({"provider": provider_name, "model": model_name})
    return members


def group_has_cycle(candidate_name: str, members: List[Dict[str, str]], groups: Dict[str, Any], seen: Optional[set[str]] = None) -> bool:
    seen = seen or set()
    for member in members:
        nested_name = member.get("group")
        if not nested_name:
            continue
        if nested_name == candidate_name:
            return True
        if nested_name in seen:
            continue
        seen.add(nested_name)
        nested_group = groups.get(nested_name)
        if isinstance(nested_group, dict):
            nested_members = nested_group.get("members", [])
            if isinstance(nested_members, list) and group_has_cycle(candidate_name, nested_members, groups, seen):
                return True
    return False


def save_group(group_name: str, description: str, strategy: str, members: List[Dict[str, str]], original_name: Optional[str] = None) -> None:
    clean_name = group_name.strip()
    if not clean_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Group name is required")
    clean_strategy = strategy.strip() or "round_robin"
    if clean_strategy not in {"round_robin", "fallback"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Strategy must be round_robin or fallback")
    if not members:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one group member is required")
    with config_lock:
        data = config_data
        groups = data.setdefault("groups", {})
        if not isinstance(groups, dict):
            groups = {}
            data["groups"] = groups
        provider_map = {provider.get("name"): provider for provider in data.get("providers", []) if isinstance(provider, dict)}
        for member in members:
            nested_group = member.get("group")
            if nested_group:
                if nested_group not in groups or not isinstance(groups.get(nested_group), dict):
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Group '{nested_group}' does not exist")
                continue
            provider_name = member.get("provider", "")
            model_name = member.get("model", "")
            provider = provider_map.get(provider_name)
            if provider is None:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider '{provider_name}' does not exist")
            models = [str(model) for model in provider.get("models", []) if model is not None]
            if model_name not in models:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Model '{model_name}' is not configured on provider '{provider_name}'")
        if group_has_cycle(clean_name, members, groups):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Group '{clean_name}' cannot contain itself, directly or through another group")
        if original_name and original_name != clean_name:
            if clean_name in groups:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Group '{clean_name}' already exists")
            groups.pop(original_name, None)
        elif not original_name and clean_name in groups:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Group '{clean_name}' already exists")
        groups[clean_name] = {
            "description": description.strip(),
            "strategy": clean_strategy,
            "members": members,
        }
        save_config(data)


def delete_group(group_name: str) -> None:
    with config_lock:
        groups = config_data.get("groups", {})
        if not isinstance(groups, dict) or group_name not in groups:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Group '{group_name}' not found")
        groups.pop(group_name)
        save_config(config_data)


async def test_group_prompt(group_name: str, prompt: str) -> Dict[str, Any]:
    endpoints = resolve_group(group_name)
    payload = {"model": None, "messages": [{"role": "user", "content": prompt}]}
    last_error = None
    for endpoint in endpoints:
        model_name = endpoint.get("model")
        if model_name:
            payload["model"] = model_name
        headers = build_provider_headers(endpoint)
        try:
            target_url = resolve_endpoint_url(endpoint)
            async with http_client.stream("POST", target_url, json=payload, headers=headers, timeout=PROVIDER_TEST_TIMEOUT_SECONDS) as response:
                body = await response.aread()
                text = body.decode("utf-8", errors="replace")
                if response.status_code == 200:
                    try:
                        parsed = json.loads(text)
                        output = json.dumps(parsed, indent=2, ensure_ascii=False)
                    except json.JSONDecodeError:
                        output = text
                    return {
                        "success": True,
                        "provider": endpoint.get("name", "unknown"),
                        "status_code": response.status_code,
                        "response": output,
                    }
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_error = f"{endpoint.get('name', 'unknown')} returned {response.status_code}"
                    continue
                return {
                    "success": False,
                    "provider": endpoint.get("name", "unknown"),
                    "status_code": response.status_code,
                    "response": text,
                }
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
            last_error = f"{endpoint.get('name', 'unknown')} failed: {exc}"
            continue
        except Exception as exc:
            last_error = f"{endpoint.get('name', 'unknown')} unexpected error: {exc}"
            continue
    return {
        "success": False,
        "provider": endpoints[-1].get("name", "unknown") if endpoints else "unknown",
        "status_code": 502,
        "response": last_error or "All provider endpoints failed",
    }


def parse_models_text(models_text: str) -> List[str]:
    return [part.strip() for part in models_text.replace(",", "\n").splitlines() if part.strip()]


def add_provider(name: str, base_url: str, api_key: str, description: str, models: List[str], extra: Optional[Dict[str, Any]] = None) -> None:
    normalized_models = [str(model).strip() for model in models if str(model).strip()]
    if not normalized_models:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one model is required for a provider")
    with config_lock:
        data = config_data
        providers = data.setdefault("providers", [])
        if not isinstance(providers, list):
            # Coerce invalid providers config to empty list to allow admin fixes via UI
            providers = []
            data["providers"] = providers
        if any(provider.get("name") == name for provider in providers if isinstance(provider, dict)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider name '{name}' already exists")
        provider_obj = {
            "name": name,
            "url": base_url,
            "api_key": api_key,
            "description": description,
            "models": normalized_models,
        }
        if extra:
            provider_obj.update(extra)
        providers.append(provider_obj)
        save_config(data)


def ensure_group_member(group_name: str, provider_name: str, model_name: str, description: str = "", strategy: str = "round_robin") -> None:
    """Ensure a provider/model pair is exposed through a model group.

    This keeps the admin Codex helper safe to click repeatedly: it creates the
    gpt-5.5 pool when missing and avoids duplicate member rows when a profile is
    edited or re-imported.
    """
    with config_lock:
        data = config_data
        groups = data.setdefault("groups", {})
        if not isinstance(groups, dict):
            groups = {}
            data["groups"] = groups
        group = groups.setdefault(group_name, {"description": description, "strategy": strategy, "members": []})
        if not isinstance(group, dict):
            group = {"description": description, "strategy": strategy, "members": []}
            groups[group_name] = group
        group.setdefault("description", description)
        group.setdefault("strategy", strategy)
        members = group.setdefault("members", [])
        if not isinstance(members, list):
            members = []
            group["members"] = members
        exists = any(isinstance(member, dict) and member.get("provider") == provider_name and member.get("model") == model_name for member in members)
        if not exists:
            members.append({"provider": provider_name, "model": model_name})
        save_config(data)


def upsert_codex_profile(name: str, access_token: str = "", refresh_token: str = "", description: str = "") -> None:
    base_provider = get_default_codex_provider()
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codex profile name is required")
    clean_access_token = access_token.strip()
    clean_refresh_token = refresh_token.strip()
    clean_description = description.strip() or base_provider["description"]
    existing = find_provider(clean_name)
    if existing is None:
        add_provider(
            name=clean_name,
            base_url=base_provider["url"],
            api_key=clean_access_token,
            description=clean_description,
            models=base_provider["models"],
            extra={
                "api_mode": base_provider["api_mode"],
                "oauth": base_provider["oauth"],
                "client_id": base_provider["client_id"],
                "client_secret": base_provider["client_secret"],
                "authorize_url": base_provider["authorize_url"],
                "token_url": base_provider["token_url"],
                "redirect_uri": base_provider["redirect_uri"],
                "scopes": base_provider["scopes"],
                "access_token": clean_access_token,
                "refresh_token": clean_refresh_token,
                "expires_at": "",
            },
        )
    else:
        with config_lock:
            existing["url"] = existing.get("url") or base_provider["url"]
            existing["description"] = clean_description
            existing["models"] = base_provider["models"]
            existing["api_mode"] = base_provider["api_mode"]
            existing["oauth"] = base_provider["oauth"]
            existing["client_id"] = existing.get("client_id") or base_provider["client_id"]
            existing["client_secret"] = existing.get("client_secret") or base_provider["client_secret"]
            existing["authorize_url"] = existing.get("authorize_url") or base_provider["authorize_url"]
            existing["token_url"] = existing.get("token_url") or base_provider["token_url"]
            existing["redirect_uri"] = existing.get("redirect_uri") or base_provider["redirect_uri"]
            existing["scopes"] = existing.get("scopes") or base_provider["scopes"]
            existing["access_token"] = clean_access_token
            existing["api_key"] = clean_access_token
            if clean_refresh_token:
                existing["refresh_token"] = clean_refresh_token
            existing["expires_at"] = ""
            save_config(config_data)
    ensure_group_member(
        "gpt-5.5",
        clean_name,
        "gpt-5.5",
        description="Balanced Codex pool for Hermes/OpenAI-compatible clients",
        strategy="round_robin",
    )


def add_codex_profile(name: str, access_token: str = "", refresh_token: str = "", description: str = "") -> None:
    if find_provider(name.strip()) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider '{name.strip()}' already exists")
    upsert_codex_profile(name=name, access_token=access_token, refresh_token=refresh_token, description=description)


def delete_provider(name: str) -> None:
    with config_lock:
        data = config_data
        providers = data.get("providers", [])
        if not isinstance(providers, list):
            # Coerce invalid providers config to empty list to allow admin fixes via UI
            providers = []
            data["providers"] = providers
        before = len(providers)
        providers = [provider for provider in providers if provider.get("name") != name]
        if len(providers) == before:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{name}' not found")
        data["providers"] = providers
        groups = data.get("groups", {})
        if isinstance(groups, dict):
            for group in groups.values():
                if not isinstance(group, dict):
                    continue
                members = group.get("members", [])
                if isinstance(members, list):
                    group["members"] = [member for member in members if member.get("provider") != name]
        save_config(data)


def provider_to_endpoint(provider: Dict[str, Any], model_name: Optional[str] = None) -> Dict[str, Any]:
    return {
        "name": provider.get("name", ""),
        "url": provider.get("url", ""),
        "api_key": provider.get("api_key", ""),
        "description": provider.get("description", ""),
        "oauth": provider.get("oauth", False),
        "api_mode": provider.get("api_mode", "openai_chat_completions"),
        "access_token": provider.get("access_token", ""),
        "refresh_token": provider.get("refresh_token", ""),
        "token_url": provider.get("token_url", ""),
        "client_id": provider.get("client_id", ""),
        "client_secret": provider.get("client_secret", ""),
        "expires_at": provider.get("expires_at", ""),
        "model": model_name or (provider.get("models", [None])[0] if provider.get("models") else None),
    }


def resolve_group(group_name: str, seen: Optional[set[str]] = None) -> List[Dict[str, Any]]:
    seen = seen or set()
    if group_name in seen:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Group nesting cycle detected at '{group_name}'")
    seen.add(group_name)
    with config_lock:
        groups = config_data.get("groups", {})
        group = groups.get(group_name)
        if not group or not isinstance(group, dict):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Model group '{group_name}' not configured")
        members = group.get("members", [])
        if not isinstance(members, list):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Invalid members configured for group '{group_name}'")
        if not members:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"No members configured for group '{group_name}'")
        provider_map = {provider.get("name"): provider for provider in config_data.get("providers", []) if isinstance(provider, dict)}
        endpoints: List[Dict[str, Any]] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            nested_group = member.get("group")
            if nested_group:
                endpoints.extend(resolve_group(str(nested_group), set(seen)))
                continue
            provider_name = member.get("provider")
            model_name = member.get("model")
            if not provider_name:
                continue
            provider = provider_map.get(provider_name)
            if not provider:
                continue
            endpoints.append(provider_to_endpoint(provider, str(model_name) if model_name else None))
    if not endpoints:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"No endpoints configured for group '{group_name}'")
    return route_endpoints(group_name, endpoints)


def resolve_requested_model(model_name: str) -> List[Dict[str, Any]]:
    """Resolve an OpenAI model name to provider endpoints.

    Explicit groups still work for aliases/pools, but direct provider model names
    are also routable. If multiple providers expose the same real model name,
    they are treated as a round-robin pool under that model name.
    """
    try:
        return resolve_group(model_name)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
    with config_lock:
        endpoints: List[Dict[str, Any]] = []
        for provider in config_data.get("providers", []):
            if not isinstance(provider, dict):
                continue
            models = [str(model) for model in provider.get("models", []) if model is not None]
            if model_name not in models:
                continue
            endpoints.append(
                {
                    "name": provider.get("name", ""),
                    "url": provider.get("url", ""),
                    "api_key": provider.get("api_key", ""),
                    "description": provider.get("description", ""),
                    "oauth": provider.get("oauth", False),
                    "api_mode": provider.get("api_mode", "openai_chat_completions"),
                    "access_token": provider.get("access_token", ""),
                    "refresh_token": provider.get("refresh_token", ""),
                    "token_url": provider.get("token_url", ""),
                    "client_id": provider.get("client_id", ""),
                    "client_secret": provider.get("client_secret", ""),
                    "expires_at": provider.get("expires_at", ""),
                    "model": model_name,
                }
            )
    if not endpoints:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Model '{model_name}' not configured")
    return route_endpoints(model_name, endpoints)


def build_playground_messages(prompt: str, image_data_url: str = "") -> List[Dict[str, Any]]:
    if image_data_url:
        content: Any = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]
    else:
        content = prompt
    return [{"role": "user", "content": content}]


def extract_chat_message_text(response_text: str) -> str:
    try:
        data = json.loads(response_text)
    except Exception:
        return response_text
    text = extract_output_from_chat_payload(data)
    return text if text else response_text


def build_curl_command(target_url: str, payload: Dict[str, Any], has_auth: bool, bearer_placeholder: str = "API_KEY") -> str:
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
    curl_flags = "-N -sS" if payload.get("stream") is True else "-sS"
    lines = [
        f"curl {curl_flags} {json.dumps(target_url)} \\",
        "  -H 'Content-Type: application/json' \\",
    ]
    if has_auth:
        lines.append("  -H 'Authorization: " + "Bearer " + bearer_placeholder + "' \\")
    lines.append("  -d @- <<'JSON'")
    return "\n".join(lines) + "\n" + payload_text + "\nJSON"


def build_aiproxy_chat_completions_url(request: Request) -> str:
    configured_base_url = os.getenv("AIPROXY_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured_base_url:
        return f"{configured_base_url}/v1/chat/completions"

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "").split(",", 1)[0].strip().rstrip("/")

    scheme = forwarded_proto or request.url.scheme
    host = forwarded_host or request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}{forwarded_prefix}/v1/chat/completions"


async def image_upload_to_data_url(image: Optional[UploadFile]) -> tuple[str, str]:
    if image is None or not image.filename:
        return "", ""
    content_type = image.content_type or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attached file must be an image")
    data = await image.read()
    if not data:
        return "", ""
    max_bytes = 8 * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Image is too large for playground upload (max 8 MiB)")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{content_type};base64,{encoded}", image.filename


def build_playground_curl_for_provider(provider_name: str, model_name: str, prompt: str, image_data_url: str = "", proxy_url: str = "/v1/chat/completions") -> str:
    provider = find_provider(provider_name)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{provider_name}' not found")
    if not model_name or model_name not in provider.get("models", []):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Model '{model_name}' is not configured for provider '{provider_name}'")
    payload = {"model": model_name, "messages": build_playground_messages(prompt, image_data_url), "stream": True}
    return build_curl_command(proxy_url, payload, True)


def set_playground_job(job_id: str, updates: Dict[str, Any]) -> None:
    with playground_jobs_lock:
        job = playground_jobs.setdefault(job_id, {})
        job.update(updates)
        job["updated_at"] = datetime.utcnow().isoformat()


def get_playground_job(job_id: str) -> Optional[Dict[str, Any]]:
    with playground_jobs_lock:
        job = playground_jobs.get(job_id)
        return dict(job) if job else None


def prune_playground_jobs(max_age_seconds: int = 3600) -> None:
    cutoff = time.time() - max_age_seconds
    with playground_jobs_lock:
        for job_id, job in list(playground_jobs.items()):
            if float(job.get("created_ts", 0)) < cutoff:
                playground_jobs.pop(job_id, None)


async def run_playground_job(job_id: str, provider: str, model: str, prompt: str, image_data_url: str) -> None:
    try:
        response = await test_provider_model(provider, model, prompt, image_data_url=image_data_url)
        set_playground_job(job_id, {
            "status": "done",
            "success": bool(response.get("success")),
            "result": response.get("response"),
            "assistant_text": response.get("assistant_text") or response.get("response"),
            "status_code": response.get("status_code"),
            "provider": response.get("provider"),
            "error": None if response.get("success") else response.get("response"),
        })
    except Exception as exc:
        set_playground_job(job_id, {
            "status": "error",
            "success": False,
            "result": str(exc),
            "assistant_text": str(exc),
            "status_code": 500,
            "error": str(exc),
        })


async def test_provider_model(provider_name: str, model_name: str, prompt: str, image_data_url: str = "") -> Dict[str, Any]:
    provider = find_provider(provider_name)
    payload = {"model": model_name, "messages": build_playground_messages(prompt, image_data_url)}
    prompt_text = truncate_text(prompt + ("\n[image attached]" if image_data_url else ""))
    started_monotonic = time.monotonic()
    started_at = datetime.utcnow().isoformat()

    def finish_log(status_code: int, provider_model: str, output: Optional[str] = None, error: Optional[str] = None, first_response_at: Optional[str] = None) -> None:
        ended_at = datetime.utcnow().isoformat()
        first_ms: Optional[float] = None
        if first_response_at:
            try:
                first_ms = (datetime.fromisoformat(first_response_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000
            except Exception:
                first_ms = None
        total_ms = (time.monotonic() - started_monotonic) * 1000
        try:
            insert_log(
                None,
                "Admin Playground",
                model_name,
                "admin-playground",
                provider_name,
                provider_model,
                status_code,
                started_at,
                first_response_at,
                ended_at,
                first_ms,
                total_ms,
                prompt_text,
                output,
                error,
            )
        except Exception:
            # Playground logging must never break the admin test request itself.
            pass

    if provider is None:
        message = f"Provider '{provider_name}' not found"
        finish_log(404, model_name or "unknown", error=message)
        return {"success": False, "provider": provider_name, "status_code": 404, "response": message, "assistant_text": message}
    if not model_name or model_name not in provider.get("models", []):
        message = f"Model '{model_name}' is not configured for provider '{provider_name}'"
        finish_log(400, model_name or "unknown", error=message)
        return {"success": False, "provider": provider_name, "status_code": 400, "response": message, "assistant_text": message}
    endpoint = provider_to_endpoint(provider, model_name)
    api_mode = str(endpoint.get("api_mode", "openai_chat_completions"))
    if api_mode in {"openai_responses", "codex_responses"}:
        target_url = resolve_responses_url(endpoint)
    else:
        target_url = resolve_endpoint_url(endpoint)
    headers = build_provider_headers(endpoint)
    curl_command = build_curl_command(target_url, payload, bool(headers.get("Authorization")))
    try:
        await ensure_provider_token(endpoint)
        if api_mode in {"openai_responses", "codex_responses"}:
            response = await send_responses_adapter(endpoint, payload)
            first_response_at = datetime.utcnow().isoformat()
            text = response.content.decode("utf-8", errors="replace")
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and not looks_like_html_response(text, content_type):
                try:
                    parsed = response.json()
                    output = json.dumps(parsed, indent=2, ensure_ascii=False)
                    assistant_text = extract_response_text(parsed)
                except Exception:
                    assistant_text = extract_response_text_from_sse(text) or text
                    output = text
                finish_log(response.status_code, model_name, output=assistant_text or output, first_response_at=first_response_at)
                return {"success": True, "provider": provider_name, "status_code": response.status_code, "response": output, "assistant_text": assistant_text, "curl_command": curl_command}
            error_text = provider_html_error(api_mode, text) if looks_like_html_response(text, content_type) else text
            finish_log(response.status_code, model_name, error=error_text, first_response_at=first_response_at)
            return {"success": False, "provider": provider_name, "status_code": response.status_code, "response": error_text, "assistant_text": error_text, "curl_command": curl_command}

        async with http_client.stream("POST", target_url, json=payload, headers=headers, timeout=PROVIDER_TEST_TIMEOUT_SECONDS) as response:
            body = await response.aread()
            first_response_at = datetime.utcnow().isoformat()
            text = body.decode("utf-8", errors="replace")
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and not looks_like_html_response(text, content_type):
                try:
                    parsed = json.loads(text)
                    output = json.dumps(parsed, indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    output = text
                assistant_text = extract_chat_message_text(text)
                finish_log(response.status_code, model_name, output=assistant_text or output, first_response_at=first_response_at)
                return {"success": True, "provider": provider_name, "status_code": response.status_code, "response": output, "assistant_text": assistant_text, "curl_command": curl_command}
            error_text = provider_html_error(api_mode, text) if looks_like_html_response(text, content_type) else text
            finish_log(response.status_code, model_name, error=error_text, first_response_at=first_response_at)
            return {"success": False, "provider": provider_name, "status_code": response.status_code, "response": error_text, "assistant_text": error_text, "curl_command": curl_command}
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
        message = str(exc)
        finish_log(502, model_name, error=message)
        return {"success": False, "provider": provider_name, "status_code": 502, "response": message, "assistant_text": message, "curl_command": curl_command}
    except Exception as exc:
        message = str(exc)
        finish_log(500, model_name, error=message)
        return {"success": False, "provider": provider_name, "status_code": 500, "response": message, "assistant_text": message, "curl_command": curl_command}


async def test_provider_candidate(name: str, base_url: str, api_key: str, models_text: str, prompt: str = "Reply with OK.") -> Dict[str, Any]:
    """Test an unsaved OpenAI-compatible provider candidate from the admin form."""
    clean_name = name.strip() or "candidate-provider"
    clean_base_url = base_url.strip().rstrip("/")
    if not clean_base_url:
        return {"success": False, "provider": clean_name, "status_code": 400, "response": "Base URL is required"}
    models = parse_models_text(models_text)
    if not models:
        models = await discover_models(clean_base_url, api_key.strip())
    if not models:
        return {"success": False, "provider": clean_name, "status_code": 400, "response": "No model configured or discovered for this provider"}
    model_name = models[0]
    provider = {
        "name": clean_name,
        "url": clean_base_url,
        "api_key": api_key.strip(),
        "models": models,
        "api_mode": "openai_chat_completions",
    }
    payload = {"model": model_name, "messages": [{"role": "user", "content": prompt or "Reply with OK."}], "stream": False}
    headers = build_provider_headers(provider)
    try:
        target_url = resolve_endpoint_url(provider)
        async with http_client.stream("POST", target_url, json=payload, headers=headers, timeout=PROVIDER_TEST_TIMEOUT_SECONDS) as response:
            body = await response.aread()
            text = body.decode("utf-8", errors="replace")
            preview = text[:4000]
            if response.status_code == 200:
                try:
                    parsed = json.loads(text)
                    output = json.dumps(parsed, indent=2, ensure_ascii=False)[:4000]
                except json.JSONDecodeError:
                    output = preview
                return {"success": True, "provider": clean_name, "model": model_name, "status_code": response.status_code, "response": output}
            return {"success": False, "provider": clean_name, "model": model_name, "status_code": response.status_code, "response": preview}
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
        return {"success": False, "provider": clean_name, "model": model_name, "status_code": 502, "response": str(exc)}
    except Exception as exc:
        return {"success": False, "provider": clean_name, "model": model_name, "status_code": 500, "response": str(exc)}


def resolve_endpoint_url(endpoint: Dict[str, Any]) -> str:
    raw_url = str(endpoint.get("url", "") or "").strip()
    if not raw_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endpoint URL is missing")

    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endpoint URL is invalid")

    path = parsed.path or ""
    normalized_path = path.rstrip("/")
    if "chat/completions" not in normalized_path.lower():
        if normalized_path == "":
            normalized_path = "/v1/chat/completions"
        elif normalized_path.endswith("/v1") or normalized_path.endswith("/openai/v1") or normalized_path.endswith("/v1beta2") or normalized_path.endswith("/v1beta3"):
            normalized_path = normalized_path + "/chat/completions"
    new_parsed = parsed._replace(path=normalized_path)
    return urlunparse(new_parsed)


def build_provider_headers(endpoint: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "User-Agent": "llm-proxy/1.0",
    }
    api_key = endpoint.get("api_key") or endpoint.get("access_token")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def build_oauth_authorize_url(request: Request, provider: Dict[str, Any], state: str) -> str:
    authorize_url = str(provider.get("authorize_url", ""))
    if not authorize_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth provider does not have authorize_url configured")
    callback = get_oauth_callback_url(request, provider.get("name", ""), provider)
    scope = provider.get("scopes")
    if isinstance(scope, list):
        scope = " ".join(str(item) for item in scope if item)
    elif scope is None:
        scope = ""
    query_params = {
        "client_id": provider.get("client_id", ""),
        "response_type": "code",
        "redirect_uri": callback,
        "state": state,
    }
    if scope:
        query_params["scope"] = scope
    query = urlencode({key: str(value) for key, value in query_params.items() if value is not None})
    return authorize_url + ("&" if "?" in authorize_url else "?") + query


async def exchange_oauth_code(request: Request, provider: Dict[str, Any], code: str) -> Dict[str, Any]:
    token_url = str(provider.get("token_url", ""))
    if not token_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OAuth provider does not have token_url configured")
    callback = get_oauth_callback_url(request, provider.get("name", ""), provider)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback,
        "client_id": provider.get("client_id", ""),
        "client_secret": provider.get("client_secret", ""),
    }
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    response = await http_client.post(token_url, data=data, headers=headers, timeout=30.0)
    try:
        token_data = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Invalid token response from OAuth provider: {exc}")
    access_token = token_data.get("access_token") or token_data.get("token")
    if not access_token:
        raise HTTPException(status_code=502, detail=f"OAuth token response missing access_token: {token_data}")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")
    result = {"access_token": access_token}
    if refresh_token:
        result["refresh_token"] = refresh_token
    if expires_in is not None:
        try:
            expires_at = datetime.utcnow().timestamp() + float(expires_in)
            result["expires_at"] = datetime.utcfromtimestamp(expires_at).isoformat()
        except Exception:
            pass
    return result



def generate_pkce_pair() -> Dict[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return {"code_verifier": verifier, "code_challenge": challenge}


async def request_codex_device_code(client_id: str) -> Dict[str, Any]:
    issuer = "https://auth.openai.com"
    response = await http_client.post(
        f"{issuer}/api/accounts/deviceauth/usercode",
        json={"client_id": client_id},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Codex device-code request failed: {response.status_code} {response.text}")
    data = response.json()
    return {
        "verification_url": f"{issuer}/codex/device",
        "user_code": data.get("user_code") or data.get("usercode"),
        "device_auth_id": data.get("device_auth_id"),
        "interval": int(str(data.get("interval") or "5")),
    }


async def poll_codex_device_authorization(device_auth_id: str, user_code: str) -> Optional[Dict[str, Any]]:
    issuer = "https://auth.openai.com"
    response = await http_client.post(
        f"{issuer}/api/accounts/deviceauth/token",
        json={"device_auth_id": device_auth_id, "user_code": user_code},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30.0,
    )
    if response.status_code in {403, 404}:
        return None
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Codex device authorization failed: {response.status_code} {response.text}")
    return response.json()


async def exchange_codex_device_code(client_id: str, authorization_code: str, code_verifier: str) -> Dict[str, Any]:
    issuer = "https://auth.openai.com"
    redirect_uri = f"{issuer}/deviceauth/callback"
    response = await http_client.post(
        f"{issuer}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Codex token exchange failed: {response.status_code} {response.text}")
    token_data = response.json()
    if not token_data.get("access_token"):
        raise HTTPException(status_code=502, detail="Codex token exchange did not return access_token")
    return token_data

def parse_expires_at(value: Any) -> Optional[float]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def provider_token_expiring(provider: Dict[str, Any], skew_seconds: int = 60) -> bool:
    expires_at = parse_expires_at(provider.get("expires_at"))
    return expires_at is not None and expires_at <= time.time() + skew_seconds


async def refresh_provider_token(provider_name: str, provider: Dict[str, Any]) -> None:
    refresh_token = provider.get("refresh_token")
    token_url = provider.get("token_url")
    client_id = provider.get("client_id")
    if not refresh_token or not token_url or not client_id:
        return
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id}
    if provider.get("client_secret"):
        data["client_secret"] = provider.get("client_secret")
    response = await http_client.post(str(token_url), data=data, headers={"Accept": "application/json"}, timeout=30.0)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Token refresh failed for {provider_name}: {response.text}")
    token_data = response.json()
    access_token = token_data.get("access_token") or token_data.get("token")
    if not access_token:
        raise HTTPException(status_code=502, detail=f"Token refresh response for {provider_name} did not include access_token")
    with config_lock:
        live_provider = find_provider(provider_name)
        if live_provider is None:
            return
        live_provider["access_token"] = access_token
        live_provider["api_key"] = access_token
        provider["access_token"] = access_token
        provider["api_key"] = access_token
        if token_data.get("refresh_token"):
            live_provider["refresh_token"] = token_data["refresh_token"]
            provider["refresh_token"] = token_data["refresh_token"]
        if token_data.get("expires_in") is not None:
            expires_at = datetime.utcnow() + timedelta(seconds=float(token_data["expires_in"]))
            live_provider["expires_at"] = expires_at.isoformat()
            provider["expires_at"] = live_provider["expires_at"]
        save_config(config_data)


async def ensure_provider_token(endpoint: Dict[str, Any]) -> None:
    if endpoint.get("oauth") and provider_token_expiring(endpoint):
        await refresh_provider_token(str(endpoint.get("name", "")), endpoint)


def route_endpoints(group_name: str, endpoints: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    with config_lock:
        strategy = str(config_data.get("groups", {}).get(group_name, {}).get("strategy", "round_robin"))
    if strategy != "round_robin" or len(endpoints) <= 1:
        return endpoints
    with route_counters_lock:
        index = route_counters.get(group_name, 0) % len(endpoints)
        route_counters[group_name] = route_counters.get(group_name, 0) + 1
    return endpoints[index:] + endpoints[:index]


def resolve_responses_url(endpoint: Dict[str, Any]) -> str:
    raw_url = str(endpoint.get("url", "") or "").strip().rstrip("/")
    if not raw_url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endpoint URL is missing")
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endpoint URL is invalid")
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/responses"):
        path += "/responses"
    return urlunparse(parsed._replace(path=path))


def extract_response_text_from_sse(body: bytes | str) -> str:
    text_body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    texts: List[str] = []
    current_event = ""
    for line in text_body.splitlines():
        line = line.strip()
        if line.startswith("event:"):
            current_event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            if not line:
                current_event = ""
            continue
        data_text = line[5:].strip()
        if not data_text or data_text == "[DONE]":
            continue
        try:
            data = json.loads(data_text)
        except Exception:
            continue
        if isinstance(data, dict):
            event_type = str(data.get("type") or current_event or "")
            # Responses streams often send both incremental delta events and final
            # completed/snapshot events. Only append top-level text fields for
            # delta events; otherwise the final event duplicates the whole answer.
            is_delta_event = "delta" in event_type
            if is_delta_event:
                value = data.get("delta")
                if not isinstance(value, str):
                    value = data.get("text")
                if not isinstance(value, str):
                    value = data.get("output_text")
                if isinstance(value, str):
                    texts.append(value)
                    continue
            if isinstance(data.get("choices"), list):
                for choice in data["choices"]:
                    delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                        texts.append(delta["content"])
            elif not event_type and isinstance(data.get("content"), list):
                for content in data["content"]:
                    if isinstance(content, dict) and isinstance(content.get("text"), str):
                        texts.append(content["text"])
    return "".join(texts)


def extract_response_text(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    texts: List[str] = []
    for item in data.get("output", []) if isinstance(data.get("output"), list) else []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) if isinstance(item.get("content"), list) else []:
            if isinstance(content, dict):
                text = content.get("text") or content.get("value")
                if text:
                    texts.append(str(text))
    if texts:
        return "".join(texts)
    if isinstance(data.get("choices"), list) and data["choices"]:
        choice = data["choices"][0]
        msg = choice.get("message", {}) if isinstance(choice, dict) else {}
        if isinstance(msg, dict) and msg.get("content"):
            return str(msg["content"])
    return json.dumps(data, ensure_ascii=False)


def looks_like_html_response(text: str, content_type: str = "") -> bool:
    lower_type = (content_type or "").lower()
    stripped = (text or "").lstrip().lower()
    return "text/html" in lower_type or stripped.startswith("<html") or stripped.startswith("<!doctype html")


def looks_like_cloudflare_challenge(text: str) -> bool:
    lower = (text or "").lower()
    return "_cf_chl_opt" in lower or "challenge-platform" in lower or "enable javascript and cookies to continue" in lower


def codex_html_auth_error(text: str = "") -> str:
    if looks_like_cloudflare_challenge(text):
        return "Codex OAuth request was blocked by a ChatGPT/Cloudflare browser challenge instead of returning JSON/SSE. Reauthenticate this provider on /admin/providers; if it still happens, ChatGPT is blocking server-side Codex requests from this deployment/IP."
    return "Codex OAuth returned an HTML login/refresh page instead of JSON/SSE. Click Reauthenticate for this provider on /admin/providers, then try again."


def provider_html_error(api_mode: str, text: str = "") -> str:
    if api_mode == "codex_responses":
        return codex_html_auth_error(text)
    if looks_like_cloudflare_challenge(text):
        return "Provider returned a Cloudflare/browser challenge HTML page instead of an OpenAI-compatible JSON response."
    return "Provider returned HTML instead of an OpenAI-compatible JSON response."


def chat_completion_from_text(model: str, text: str) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


def sse_chat_chunks(model: str, text: str):
    chunk_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    first = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode("utf-8")
    body = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
    yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8")
    end = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield f"data: {json.dumps(end, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def chat_to_responses_payload(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
    messages = payload.get("messages", [])
    input_messages = []
    instructions: Optional[str] = None
    if payload.get("instructions"):
        instructions = str(payload.get("instructions"))
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content", "")
        if role == "system":
            instructions = (instructions + "\n" if instructions else "") + str(content)
            continue
        input_messages.append({"role": role, "content": content})
    converted: Dict[str, Any] = {"model": model, "input": input_messages or str(payload.get("prompt", ""))}
    converted["instructions"] = instructions or "You are a helpful assistant."
    # Codex/ChatGPT Responses requires explicit non-storage. Default to false so
    # OpenAI-compatible callers do not need to know about the Responses API quirk.
    converted["store"] = bool(payload.get("store")) if payload.get("store") is not None else False
    converted["stream"] = True
    if payload.get("temperature") is not None:
        converted["temperature"] = payload.get("temperature")
    return converted


async def send_responses_adapter(endpoint: Dict[str, Any], payload: Dict[str, Any]) -> httpx.Response:
    model = str(endpoint.get("model") or payload.get("model") or "")
    converted = chat_to_responses_payload(payload, model)
    target_url = resolve_responses_url(endpoint)
    return await http_client.post(target_url, json=converted, headers=build_provider_headers(endpoint), timeout=UPSTREAM_REQUEST_TIMEOUT_SECONDS)


async def stream_provider_response(response: httpx.Response):
    async for chunk in response.aiter_bytes():
        if chunk:
            yield chunk

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Proxy deluje!"}

@app.get("/v1/models")
async def list_openai_models(api_key_record: sqlite3.Row = Depends(validate_api_key)) -> Dict[str, Any]:
    del api_key_record
    model_ids: List[str] = []
    with config_lock:
        for provider in config_data.get("providers", []):
            if not isinstance(provider, dict):
                continue
            for model in provider.get("models", []):
                model_id = str(model).strip()
                if model_id and model_id not in model_ids:
                    model_ids.append(model_id)
        groups = config_data.get("groups", {})
        if isinstance(groups, dict):
            for group_name in groups:
                if group_name not in model_ids:
                    model_ids.append(str(group_name))
    return {"object": "list", "data": [{"id": model_id, "object": "model", "created": 0, "owned_by": "simple-aiproxy"} for model_id in model_ids]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, background_tasks: BackgroundTasks, api_key_record: sqlite3.Row = Depends(validate_api_key)) -> Response:
    del background_tasks
    payload = await request.json()
    requested_model = str(payload.get("model") or "")
    if not requested_model:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing model in request payload")
    endpoints = resolve_requested_model(requested_model)
    api_key_value = api_key_record["key"]
    api_key_name = api_key_record["name"] if "name" in api_key_record.keys() else None
    prompt_text = extract_prompt(payload)
    started_monotonic = time.monotonic()
    started_at = datetime.utcnow().isoformat()
    last_error: Optional[str] = None
    fallback_statuses = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}

    def timing(first_at: Optional[str] = None) -> tuple[str, Optional[float], float]:
        ended = datetime.utcnow().isoformat()
        total_ms = (time.monotonic() - started_monotonic) * 1000
        first_ms: Optional[float] = None
        if first_at:
            try:
                first_ms = (datetime.fromisoformat(first_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000
            except Exception:
                first_ms = None
        return ended, first_ms, total_ms

    for endpoint in endpoints:
        provider_name = str(endpoint.get("name", "unknown"))
        provider_model = str(endpoint.get("model") or "")
        provider_payload = prepare_provider_chat_payload(payload, endpoint, provider_model)
        try:
            await ensure_provider_token(endpoint)
            api_mode = str(endpoint.get("api_mode", "openai_chat_completions"))
            if api_mode in {"openai_responses", "codex_responses"}:
                response = await send_responses_adapter(endpoint, provider_payload)
                content = response.content
                first_response_at = datetime.utcnow().isoformat()
                content_type = response.headers.get("content-type", "")
                raw_text = content.decode("utf-8", errors="replace")
                if looks_like_html_response(raw_text, content_type):
                    error_msg = provider_html_error(api_mode, raw_text)
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, 502, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, None, error_msg)
                    last_error = f"{provider_name} returned HTML challenge: {error_msg}"
                    continue
                if response.status_code == 200:
                    try:
                        text = extract_response_text(response.json())
                    except Exception:
                        text = extract_response_text_from_sse(content) or raw_text
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, 200, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, text, None)
                    if payload.get("stream"):
                        return StreamingResponse(sse_chat_chunks(provider_model, text), status_code=200, media_type="text/event-stream")
                    return Response(json.dumps(chat_completion_from_text(provider_model, text), ensure_ascii=False), status_code=200, media_type="application/json")
                error_msg = raw_text
                if response.status_code in fallback_statuses:
                    last_error = f"{provider_name} returned {response.status_code}: {error_msg}"
                    continue
                ended_at, first_ms, total_ms = timing(first_response_at)
                insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, response.status_code, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, None, error_msg)
                return Response(content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))

            headers = build_provider_headers(endpoint)
            target_url = resolve_endpoint_url(endpoint)
            request_obj = http_client.build_request("POST", target_url, json=provider_payload, headers=headers)
            response = await http_client.send(request_obj, stream=True)
            content_type = response.headers.get("content-type", "application/json")
            if response.status_code == 200:
                if payload.get("stream"):
                    async def proxy_stream() -> Any:
                        first_response_at: Optional[str] = None
                        captured = bytearray()
                        error_msg: Optional[str] = None
                        try:
                            async for chunk in response.aiter_bytes():
                                if chunk and first_response_at is None:
                                    first_response_at = datetime.utcnow().isoformat()
                                if chunk and len(captured) < 12000:
                                    captured.extend(chunk[: 12000 - len(captured)])
                                yield chunk
                        except Exception as exc:
                            error_msg = str(exc)
                            raise
                        finally:
                            await response.aclose()
                            ended_at, first_ms, total_ms = timing(first_response_at)
                            output_text = extract_output_from_body(bytes(captured), content_type) if captured else ""
                            insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, 200 if error_msg is None else 502, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, output_text, error_msg)
                    return StreamingResponse(proxy_stream(), status_code=200, media_type=content_type)
                content = await response.aread()
                await response.aclose()
                first_response_at = datetime.utcnow().isoformat()
                raw_text = content.decode("utf-8", errors="replace")
                if looks_like_html_response(raw_text, content_type):
                    error_msg = provider_html_error(api_mode, raw_text)
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, 502, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, None, error_msg)
                    last_error = f"{provider_name} returned HTML challenge: {error_msg}"
                    continue
                ended_at, first_ms, total_ms = timing(first_response_at)
                output_text = extract_output_from_body(content, content_type)
                insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, response.status_code, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, output_text, None)
                return Response(content, status_code=200, media_type=content_type)
            content = await response.aread()
            await response.aclose()
            first_response_at = datetime.utcnow().isoformat()
            error_msg = content.decode("utf-8", errors="replace")
            if response.status_code in fallback_statuses:
                last_error = f"{provider_name} returned {response.status_code}: {error_msg}"
                continue
            ended_at, first_ms, total_ms = timing(first_response_at)
            insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, response.status_code, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, None, error_msg)
            return Response(content, status_code=response.status_code, media_type=content_type)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = f"{provider_name} failed: {format_provider_exception(exc)}"
            continue
        except Exception as exc:
            last_error = f"{provider_name} unexpected error: {exc}"
            continue
    ended_at, first_ms, total_ms = timing(None)
    last_endpoint = endpoints[-1] if endpoints else {}
    insert_log(api_key_value, api_key_name, requested_model, requested_model, str(last_endpoint.get("name", "unknown")), str(last_endpoint.get("model", "unknown")), 502, started_at, None, ended_at, first_ms, total_ms, prompt_text, None, last_error)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=last_error or "All provider endpoints failed")

@app.get("/admin/keys", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_keys(request: Request) -> Any:
    return templates.TemplateResponse(request=request, name="keys.html", context={"api_keys": list_api_keys()})


@app.post("/admin/keys/new", dependencies=[Depends(verify_admin)])
async def admin_keys_new(name: str = Form(...)) -> RedirectResponse:
    create_api_key(name)
    return RedirectResponse(url="/admin/keys", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/logs", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_logs(request: Request) -> Any:
    return templates.TemplateResponse(request=request, name="logs.html", context={"logs": list_logs()})


@app.get("/admin/config", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_config(request: Request, message: Optional[str] = None) -> Any:
    with config_lock:
        yaml_text = yaml.safe_dump(config_data, sort_keys=False)
    return templates.TemplateResponse(
        request=request,
        name="config.html",
        context={"yaml_text": yaml_text, "message": message},
    )


@app.post("/admin/config/save", dependencies=[Depends(verify_admin)])
async def admin_config_save(yaml_text: str = Form(...)) -> RedirectResponse:
    try:
        parsed = yaml.safe_load(yaml_text)
        if parsed is None:
            parsed = {}
        save_config(parsed)
        return RedirectResponse(url="/admin/config", status_code=status.HTTP_303_SEE_OTHER)
    except yaml.YAMLError as exc:
        return HTMLResponse(content=f"Invalid YAML: {exc}", status_code=400)


@app.get("/admin/groups", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_groups(request: Request, message: Optional[str] = None) -> Any:
    return templates.TemplateResponse(
        request=request,
        name="groups.html",
        context={"groups": get_groups(), "providers": get_providers(), "message": message},
    )


@app.post("/admin/groups", dependencies=[Depends(verify_admin)])
async def admin_groups_add(
    name: str = Form(...),
    description: str = Form(""),
    strategy: str = Form("round_robin"),
    member_provider: List[str] = Form(default=[]),
    member_model: List[str] = Form(default=[]),
    member_type: List[str] = Form(default=[]),
    member_group: List[str] = Form(default=[]),
) -> RedirectResponse:
    members = normalize_group_members(member_provider, member_model, member_type, member_group)
    save_group(name, description, strategy, members)
    return RedirectResponse(url=f"/admin/groups?message=Created+group:+{quote(name.strip())}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/groups/{group_name}/save", dependencies=[Depends(verify_admin)])
async def admin_groups_save(
    group_name: str,
    name: str = Form(...),
    description: str = Form(""),
    strategy: str = Form("round_robin"),
    member_provider: List[str] = Form(default=[]),
    member_model: List[str] = Form(default=[]),
    member_type: List[str] = Form(default=[]),
    member_group: List[str] = Form(default=[]),
) -> RedirectResponse:
    members = normalize_group_members(member_provider, member_model, member_type, member_group)
    save_group(name, description, strategy, members, original_name=group_name)
    return RedirectResponse(url=f"/admin/groups?message=Saved+group:+{quote(name.strip())}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/groups/{group_name}/delete", dependencies=[Depends(verify_admin)])
async def admin_groups_delete(group_name: str) -> RedirectResponse:
    delete_group(group_name)
    return RedirectResponse(url=f"/admin/groups?message=Deleted+group:+{quote(group_name)}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/providers", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_providers(request: Request, message: Optional[str] = None) -> Any:
    providers = get_providers()
    return templates.TemplateResponse(
        request=request,
        name="providers.html",
        context={
            "providers": providers,
            "message": message,
            "codex_exists": any(provider.get("name") == "codex" for provider in providers),
        },
    )


@app.post("/admin/providers/codex-device/start", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def admin_providers_codex_device_start(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    replace_existing: bool = Form(False),
) -> Any:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codex profile name is required")
    if find_provider(clean_name) is not None and not replace_existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider '{clean_name}' already exists")
    client_id = get_default_codex_provider()["client_id"]
    device = await request_codex_device_code(client_id)
    if not device.get("user_code") or not device.get("device_auth_id"):
        raise HTTPException(status_code=502, detail=f"Invalid Codex device-code response: {device}")
    login_id = uuid.uuid4().hex
    oauth_state_store[login_id] = {
        "provider": clean_name,
        "description": description.strip(),
        "created_at": datetime.utcnow().isoformat(),
        "client_id": client_id,
        "device_auth_id": device["device_auth_id"],
        "user_code": device["user_code"],
        "interval": device["interval"],
        "replace_existing": replace_existing,
    }
    return templates.TemplateResponse(
        request=request,
        name="codex_device.html",
        context={
            "login_id": login_id,
            "profile_name": clean_name,
            "verification_url": device["verification_url"],
            "user_code": device["user_code"],
            "interval": device["interval"],
        },
    )


@app.get("/admin/providers/codex-device/{login_id}/status", dependencies=[Depends(verify_admin)])
async def admin_providers_codex_device_status(login_id: str) -> JSONResponse:
    state_data = oauth_state_store.get(login_id)
    if not state_data:
        return JSONResponse({"status": "expired", "message": "Login state expired or already completed."}, status_code=404)
    created = datetime.fromisoformat(state_data["created_at"])
    if datetime.utcnow() - created > timedelta(minutes=15):
        oauth_state_store.pop(login_id, None)
        return JSONResponse({"status": "expired", "message": "Device code expired. Start again."}, status_code=410)
    code_resp = await poll_codex_device_authorization(state_data["device_auth_id"], state_data["user_code"])
    if code_resp is None:
        return JSONResponse({"status": "pending", "message": "Waiting for OpenAI device login..."})
    authorization_code = code_resp.get("authorization_code")
    code_verifier = code_resp.get("code_verifier")
    if not authorization_code or not code_verifier:
        raise HTTPException(status_code=502, detail=f"Codex device auth response missing authorization_code/code_verifier: {code_resp}")
    tokens = await exchange_codex_device_code(state_data["client_id"], authorization_code, code_verifier)
    upsert_codex_profile(
        name=state_data["provider"],
        access_token=tokens.get("access_token", ""),
        refresh_token=tokens.get("refresh_token", ""),
        description=state_data.get("description", ""),
    )
    oauth_state_store.pop(login_id, None)
    action = "re-authenticated" if state_data.get("replace_existing") else "logged in and added"
    return JSONResponse({"status": "complete", "message": f"Codex profile '{state_data['provider']}' {action} for gpt-5.5."})


@app.post("/admin/providers/codex-token", response_class=RedirectResponse, dependencies=[Depends(verify_admin)])
async def admin_providers_add_codex_token(
    name: str = Form(...),
    access_token: str = Form(""),
    refresh_token: str = Form(""),
    description: str = Form(""),
) -> RedirectResponse:
    add_codex_profile(name=name, access_token=access_token, refresh_token=refresh_token, description=description)
    return RedirectResponse(url=f"/admin/providers?message=Added+Codex+profile+and+gpt-5.5+pool+member:+{quote(name.strip())}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/providers/{name}/oauth/login", dependencies=[Depends(verify_admin)])
def admin_provider_oauth_login(name: str, request: Request) -> RedirectResponse:
    provider = find_provider(name)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{name}' not found")
    if not provider.get("oauth"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider '{name}' is not configured for OAuth")
    authorize_url = provider.get("authorize_url")
    client_id = provider.get("client_id")
    if not authorize_url or not client_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider '{name}' OAuth configuration is incomplete")
    state = uuid.uuid4().hex
    oauth_state_store[state] = {"provider": name, "created_at": datetime.utcnow().isoformat()}
    redirect_url = build_oauth_authorize_url(request, provider, state)
    return RedirectResponse(url=redirect_url)


@app.get("/admin/providers/{name}/oauth/callback")
async def admin_provider_oauth_callback(name: str, request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    if error:
        return RedirectResponse(url=f"/admin/providers?message=OAuth+error:+{quote(error)}")
    if not code or not state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing OAuth code or state")
    stored = oauth_state_store.pop(state, None)
    if not stored or stored.get("provider") != name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OAuth state")
    provider = find_provider(name)
    if provider is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Provider '{name}' not found")
    token_data = await exchange_oauth_code(request, provider, code)
    with config_lock:
        provider["access_token"] = token_data.get("access_token")
        if token_data.get("refresh_token"):
            provider["refresh_token"] = token_data.get("refresh_token")
        if token_data.get("expires_at"):
            provider["expires_at"] = token_data.get("expires_at")
        provider["api_key"] = token_data.get("access_token")
        save_config(config_data)
    return RedirectResponse(url="/admin/providers?message=OAuth+login+completed")


@app.post("/admin/providers/test", dependencies=[Depends(verify_admin)])
async def admin_providers_test(
    name: str = Form(""),
    base_url: str = Form(...),
    api_key: str = Form(""),
    models: str = Form(""),
    prompt: str = Form("Reply with OK."),
) -> JSONResponse:
    result = await test_provider_candidate(name=name, base_url=base_url, api_key=api_key, models_text=models, prompt=prompt)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


@app.post("/admin/providers", dependencies=[Depends(verify_admin)])
async def admin_providers_add(
    name: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(""),
    description: str = Form(""),
    models: str = Form(""),
) -> RedirectResponse:
    discovered = parse_models_text(models)
    if not discovered:
        discovered = await discover_models(base_url.strip(), api_key.strip())
    if not discovered:
        discovered = [name.strip()]
    add_provider(name=name.strip(), base_url=base_url.strip(), api_key=api_key.strip(), description=description.strip(), models=discovered)
    return RedirectResponse(url="/admin/providers", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/providers/ollama", dependencies=[Depends(verify_admin)])
async def admin_providers_add_ollama(
    name: str = Form(...),
    base_url: str = Form("http://localhost:11434/v1"),
    models: str = Form(...),
    description: str = Form(""),
) -> RedirectResponse:
    model_list = parse_models_text(models)
    if not model_list:
        model_list = await discover_models(base_url.strip(), "")
    add_provider(
        name=name.strip(),
        base_url=base_url.strip().rstrip("/") or "http://localhost:11434/v1",
        api_key="",
        description=description.strip() or "Ollama OpenAI-compatible local provider",
        models=model_list,
        extra={"api_mode": "openai_chat_completions"},
    )
    return RedirectResponse(url=f"/admin/providers?message=Added+Ollama+provider:+{quote(name.strip())}", status_code=status.HTTP_303_SEE_OTHER)


@app.delete("/admin/providers/{name}", dependencies=[Depends(verify_admin)])
async def admin_providers_delete(name: str) -> Dict[str, Any]:
    delete_provider(name)
    return {"status": "ok", "message": f"Provider '{name}' deleted."}


@app.post("/admin/providers/{name}/delete", dependencies=[Depends(verify_admin)])
async def admin_providers_delete_post(name: str) -> RedirectResponse:
    delete_provider(name)
    return RedirectResponse(url=f"/admin/providers?message=Deleted+provider:+{quote(name)}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/playground", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_playground(request: Request, result: Optional[str] = None, provider: Optional[str] = None, status_code: Optional[int] = None, error: Optional[str] = None) -> Any:
    return templates.TemplateResponse(
        request=request,
        name="playground.html",
        context={
            "providers": get_providers(),
            "result": result,
            "provider": provider,
            "selected_provider": provider,
            "status_code": status_code,
            "error": error,
        },
    )


@app.post("/admin/playground/run", dependencies=[Depends(verify_admin)])
async def admin_playground_run_async(
    request: Request,
    background_tasks: BackgroundTasks,
    provider: str = Form(...),
    model: str = Form(...),
    prompt: str = Form(...),
    image: Optional[UploadFile] = File(None),
) -> JSONResponse:
    prune_playground_jobs()
    image_data_url, image_filename = await image_upload_to_data_url(image)
    curl_command = build_playground_curl_for_provider(
        provider,
        model,
        prompt,
        image_data_url=image_data_url,
        proxy_url=build_aiproxy_chat_completions_url(request),
    )
    job_id = uuid.uuid4().hex
    with playground_jobs_lock:
        playground_jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "success": None,
            "provider": provider,
            "selected_model": model,
            "prompt": prompt,
            "image_filename": image_filename,
            "curl_command": curl_command,
            "status_code": None,
            "result": None,
            "assistant_text": None,
            "error": None,
            "created_ts": time.time(),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
    background_tasks.add_task(run_playground_job, job_id, provider, model, prompt, image_data_url)
    return JSONResponse({
        "ok": True,
        "pending": True,
        "job_id": job_id,
        "provider": provider,
        "selected_model": model,
        "prompt": prompt,
        "image_filename": image_filename,
        "curl_command": curl_command,
    })


@app.get("/admin/playground/jobs/{job_id}", dependencies=[Depends(verify_admin)])
async def admin_playground_job_status(job_id: str) -> JSONResponse:
    job = get_playground_job(job_id)
    if job is None:
        return JSONResponse({"ok": False, "status": "missing", "error": "Playground job not found or expired"}, status_code=404)
    return JSONResponse({"ok": True, **job})


@app.post("/admin/playground", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def admin_playground_run(
    request: Request,
    provider: str = Form(...),
    model: str = Form(...),
    prompt: str = Form(...),
    image: Optional[UploadFile] = File(None),
) -> Any:
    try:
        image_data_url, image_filename = await image_upload_to_data_url(image)
        response = await test_provider_model(provider, model, prompt, image_data_url=image_data_url)
        response["curl_command"] = build_playground_curl_for_provider(
            provider,
            model,
            prompt,
            image_data_url=image_data_url,
            proxy_url=build_aiproxy_chat_completions_url(request),
        )
        error = None if response.get("success") else response.get("response")
    except HTTPException as exc:
        response = {"success": False, "provider": provider, "status_code": exc.status_code, "response": str(exc.detail), "assistant_text": str(exc.detail), "curl_command": ""}
        image_filename = image.filename if image and image.filename else ""
        error = str(exc.detail)
    return templates.TemplateResponse(
        request=request,
        name="playground.html",
        context={
            "providers": get_providers(),
            "result": response.get("response"),
            "assistant_text": response.get("assistant_text"),
            "curl_command": response.get("curl_command"),
            "provider": response.get("provider"),
            "selected_model": model,
            "status_code": response.get("status_code"),
            "error": error,
            "selected_provider": provider,
            "prompt": prompt,
            "image_filename": image_filename,
        },
    )


@app.post("/admin/config/update", dependencies=[Depends(verify_admin)])
async def api_config_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    save_config(payload)
    return {"status": "ok", "message": "Configuration updated."}


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_root(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/admin/keys")
