import os
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
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
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
                group_name TEXT,
                provider_name TEXT,
                provider_model TEXT,
                status_code INTEGER,
                started_at TEXT,
                ended_at TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
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
        timeout=httpx.Timeout(30.0, connect=10.0),
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


def list_logs(limit: int = 200) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT api_key, group_name, provider_name, provider_model, status_code, started_at, ended_at, error, created_at FROM Logs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        try:
            start = datetime.fromisoformat(row["started_at"]) if row["started_at"] else None
            end = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None
            if start and end:
                row["duration"] = f"{(end - start).total_seconds():.2f}s"
            else:
                row["duration"] = "-"
        except Exception:
            row["duration"] = "-"
    return rows


def insert_log(api_key: Optional[str], group_name: str, provider_name: str, provider_model: str, status_code: int, started_at: str, ended_at: str, error: Optional[str]) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO Logs (api_key, group_name, provider_name, provider_model, status_code, started_at, ended_at, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (api_key, group_name, provider_name, provider_model, status_code, started_at, ended_at, error, datetime.utcnow().isoformat()),
        )
        conn.commit()


def create_api_key(name: str) -> str:
    token = uuid.uuid4().hex
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)", (name.strip(), token, datetime.utcnow().isoformat()))
        conn.commit()
    return token


def get_providers() -> List[Dict[str, Any]]:
    providers_list: List[Dict[str, Any]] = []
    with config_lock:
        for provider in config_data.get("providers", []):
            if not isinstance(provider, dict):
                continue
            providers_list.append(
                {
                    "name": provider.get("name", ""),
                    "url": provider.get("url", ""),
                    "api_key": provider.get("api_key", ""),
                    "description": provider.get("description", ""),
                    "models": provider.get("models", []),
                    "oauth": bool(provider.get("oauth")),
                    "authorize_url": provider.get("authorize_url", ""),
                    "token_url": provider.get("token_url", ""),
                    "redirect_uri": provider.get("redirect_uri", ""),
                    "access_token": provider.get("access_token", ""),
                    "api_mode": provider.get("api_mode", "openai_chat_completions"),
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
            groups_list.append(
                {
                    "name": group_name,
                    "description": group.get("description", ""),
                    "members": group.get("members", []),
                }
            )
    return groups_list


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
            async with http_client.stream("POST", target_url, json=payload, headers=headers, timeout=30.0) as response:
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


def add_codex_profile(name: str, access_token: str = "", refresh_token: str = "", description: str = "") -> None:
    base_provider = get_default_codex_provider()
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Codex profile name is required")
    add_provider(
        name=clean_name,
        base_url=base_provider["url"],
        api_key=access_token.strip(),
        description=description.strip() or base_provider["description"],
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
            "access_token": access_token.strip(),
            "refresh_token": refresh_token.strip(),
            "expires_at": "",
        },
    )
    ensure_group_member(
        "gpt-5.5",
        clean_name,
        "gpt-5.5",
        description="Balanced Codex pool for Hermes/OpenAI-compatible clients",
        strategy="round_robin",
    )


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


def resolve_group(group_name: str) -> List[Dict[str, Any]]:
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
            provider_name = member.get("provider")
            model_name = member.get("model")
            if not provider_name:
                continue
            provider = provider_map.get(provider_name)
            if not provider:
                continue
            endpoint = {
                "name": provider_name,
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
            endpoints.append(endpoint)
    if not endpoints:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"No endpoints configured for group '{group_name}'")
    return endpoints


async def test_provider_model(provider_name: str, model_name: str, prompt: str) -> Dict[str, Any]:
    provider = find_provider(provider_name)
    if provider is None:
        return {"success": False, "provider": provider_name, "status_code": 404, "response": f"Provider '{provider_name}' not found"}
    if not model_name or model_name not in provider.get("models", []):
        return {"success": False, "provider": provider_name, "status_code": 400, "response": f"Model '{model_name}' is not configured for provider '{provider_name}'"}
    payload = {"model": model_name, "messages": [{"role": "user", "content": prompt}]}
    headers = build_provider_headers(provider)
    try:
        target_url = resolve_endpoint_url(provider)
        async with http_client.stream("POST", target_url, json=payload, headers=headers, timeout=30.0) as response:
            body = await response.aread()
            text = body.decode("utf-8", errors="replace")
            if response.status_code == 200:
                try:
                    parsed = json.loads(text)
                    output = json.dumps(parsed, indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    output = text
                return {"success": True, "provider": provider_name, "status_code": response.status_code, "response": output}
            return {"success": False, "provider": provider_name, "status_code": response.status_code, "response": text}
    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
        return {"success": False, "provider": provider_name, "status_code": 502, "response": str(exc)}
    except Exception as exc:
        return {"success": False, "provider": provider_name, "status_code": 500, "response": str(exc)}


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
    if instructions:
        converted["instructions"] = instructions
    if payload.get("temperature") is not None:
        converted["temperature"] = payload.get("temperature")
    if payload.get("max_tokens") is not None:
        converted["max_output_tokens"] = payload.get("max_tokens")
    return converted


async def send_responses_adapter(endpoint: Dict[str, Any], payload: Dict[str, Any]) -> httpx.Response:
    model = str(endpoint.get("model") or payload.get("model") or "")
    converted = chat_to_responses_payload(payload, model)
    target_url = resolve_responses_url(endpoint)
    return await http_client.post(target_url, json=converted, headers=build_provider_headers(endpoint), timeout=60.0)


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
    with config_lock:
        groups = config_data.get("groups", {})
        data = []
        if isinstance(groups, dict):
            for group_name in groups:
                data.append({"id": group_name, "object": "model", "created": 0, "owned_by": "simple-aiproxy"})
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, background_tasks: BackgroundTasks, api_key_record: sqlite3.Row = Depends(validate_api_key)) -> Response:
    payload = await request.json()
    group_name = payload.get("model")
    if not group_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing model group in request payload")
    endpoints = route_endpoints(str(group_name), resolve_group(str(group_name)))
    api_key_value = api_key_record["key"]
    started_at = datetime.utcnow().isoformat()
    last_error: Optional[str] = None
    fallback_statuses = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}
    for endpoint in endpoints:
        provider_name = str(endpoint.get("name", "unknown"))
        provider_model = str(endpoint.get("model") or "")
        provider_payload = dict(payload)
        provider_payload["model"] = provider_model
        try:
            await ensure_provider_token(endpoint)
            api_mode = str(endpoint.get("api_mode", "openai_chat_completions"))
            if api_mode in {"openai_responses", "codex_responses"}:
                response = await send_responses_adapter(endpoint, provider_payload)
                content = response.content
                if response.status_code == 200:
                    try:
                        text = extract_response_text(response.json())
                    except Exception:
                        text = content.decode("utf-8", errors="replace")
                    ended_at = datetime.utcnow().isoformat()
                    background_tasks.add_task(insert_log, api_key_value, str(group_name), provider_name, provider_model, 200, started_at, ended_at, None)
                    if payload.get("stream"):
                        return StreamingResponse(sse_chat_chunks(provider_model, text), status_code=200, media_type="text/event-stream")
                    return Response(json.dumps(chat_completion_from_text(provider_model, text), ensure_ascii=False), status_code=200, media_type="application/json")
                error_msg = content.decode("utf-8", errors="replace")
                if response.status_code in fallback_statuses:
                    last_error = f"{provider_name} returned {response.status_code}: {error_msg}"
                    continue
                ended_at = datetime.utcnow().isoformat()
                background_tasks.add_task(insert_log, api_key_value, str(group_name), provider_name, provider_model, response.status_code, started_at, ended_at, error_msg)
                return Response(content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))

            headers = build_provider_headers(endpoint)
            target_url = resolve_endpoint_url(endpoint)
            request_obj = http_client.build_request("POST", target_url, json=provider_payload, headers=headers)
            response = await http_client.send(request_obj, stream=True)
            if response.status_code == 200:
                async def proxy_stream() -> Any:
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    finally:
                        await response.aclose()
                ended_at = datetime.utcnow().isoformat()
                background_tasks.add_task(insert_log, api_key_value, str(group_name), provider_name, provider_model, response.status_code, started_at, ended_at, None)
                return StreamingResponse(proxy_stream(), status_code=200, media_type=response.headers.get("content-type", "application/json"))
            content = await response.aread()
            await response.aclose()
            error_msg = content.decode("utf-8", errors="replace")
            if response.status_code in fallback_statuses:
                last_error = f"{provider_name} returned {response.status_code}: {error_msg}"
                continue
            ended_at = datetime.utcnow().isoformat()
            background_tasks.add_task(insert_log, api_key_value, str(group_name), provider_name, provider_model, response.status_code, started_at, ended_at, error_msg)
            return Response(content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
            last_error = f"{provider_name} failed: {exc}"
            continue
        except Exception as exc:
            last_error = f"{provider_name} unexpected error: {exc}"
            continue
    ended_at = datetime.utcnow().isoformat()
    background_tasks.add_task(insert_log, api_key_value, str(group_name), endpoints[-1].get("name", "unknown"), endpoints[-1].get("model", "unknown"), 502, started_at, ended_at, last_error)
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=last_error or "All provider endpoints failed")


@app.get("/admin/keys", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_keys(request: Request) -> Any:
    return templates.TemplateResponse("keys.html", {"request": request, "api_keys": list_api_keys()})


@app.post("/admin/keys/new", dependencies=[Depends(verify_admin)])
async def admin_keys_new(name: str = Form(...)) -> RedirectResponse:
    create_api_key(name)
    return RedirectResponse(url="/admin/keys", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/logs", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_logs(request: Request) -> Any:
    return templates.TemplateResponse("logs.html", {"request": request, "logs": list_logs()})


@app.get("/admin/config", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_config(request: Request, message: Optional[str] = None) -> Any:
    with config_lock:
        yaml_text = yaml.safe_dump(config_data, sort_keys=False)
    return templates.TemplateResponse(
        "config.html",
        {"request": request, "yaml_text": yaml_text, "message": message},
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


@app.get("/admin/providers", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_providers(request: Request, message: Optional[str] = None) -> Any:
    providers = get_providers()
    return templates.TemplateResponse(
        "providers.html",
        {
            "request": request,
            "providers": providers,
            "message": message,
            "codex_exists": any(provider.get("name") == "codex" for provider in providers),
        },
    )


@app.get("/admin/providers/add-codex", response_class=RedirectResponse, dependencies=[Depends(verify_admin)])
async def admin_providers_add_codex(request: Request) -> RedirectResponse:
    del request
    existing = {provider.get("name") for provider in get_providers()}
    name = "codex"
    suffix = 1
    while name in existing:
        suffix += 1
        name = f"codex-{suffix}"
    add_codex_profile(name=name)
    return RedirectResponse(url=f"/admin/providers?message=Added+Codex+profile+template+and+gpt-5.5+pool+member:+{quote(name)}", status_code=status.HTTP_303_SEE_OTHER)


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


@app.post("/admin/providers", dependencies=[Depends(verify_admin)])
async def admin_providers_add(
    name: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(""),
    description: str = Form(""),
) -> RedirectResponse:
    discovered = await discover_models(base_url.strip(), api_key.strip())
    if not discovered:
        discovered = [name.strip()]
    add_provider(name=name.strip(), base_url=base_url.strip(), api_key=api_key.strip(), description=description.strip(), models=discovered)
    return RedirectResponse(url="/admin/providers", status_code=status.HTTP_303_SEE_OTHER)


@app.delete("/admin/providers/{name}", dependencies=[Depends(verify_admin)])
async def admin_providers_delete(name: str) -> Dict[str, Any]:
    delete_provider(name)
    return {"status": "ok", "message": f"Provider '{name}' deleted."}


@app.get("/admin/playground", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_playground(request: Request, result: Optional[str] = None, provider: Optional[str] = None, status_code: Optional[int] = None, error: Optional[str] = None) -> Any:
    return templates.TemplateResponse(
        "playground.html",
        {
            "request": request,
            "providers": get_providers(),
            "result": result,
            "provider": provider,
            "selected_provider": provider,
            "status_code": status_code,
            "error": error,
        },
    )


@app.post("/admin/playground", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
async def admin_playground_run(
    request: Request,
    provider: str = Form(...),
    model: str = Form(...),
    prompt: str = Form(...),
) -> Any:
    response = await test_provider_model(provider, model, prompt)
    return templates.TemplateResponse(
        "playground.html",
        {
            "request": request,
            "providers": get_providers(),
            "result": response.get("response"),
            "provider": response.get("provider"),
            "selected_model": model,
            "status_code": response.get("status_code"),
            "error": None if response.get("success") else response.get("response"),
            "selected_provider": provider,
            "prompt": prompt,
        },
    )


@app.post("/admin/config/update", dependencies=[Depends(verify_admin)])
async def api_config_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    save_config(payload)
    return {"status": "ok", "message": "Configuration updated."}


@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(verify_admin)])
def admin_root(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/admin/keys")
