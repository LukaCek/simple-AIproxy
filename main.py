import asyncio
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

import httpx
import json
import secrets
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
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


def add_provider(name: str, base_url: str, api_key: str, description: str, models: List[str]) -> None:
    normalized_models = [str(model).strip() for model in models if str(model).strip()]
    if not normalized_models:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one model is required for a provider")
    with config_lock:
        data = config_data
        providers = data.setdefault("providers", [])
        if not isinstance(providers, list):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Invalid providers config")
        if any(provider.get("name") == name for provider in providers if isinstance(provider, dict)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider name '{name}' already exists")
        providers.append(
            {
                "name": name,
                "url": base_url,
                "api_key": api_key,
                "description": description,
                "models": normalized_models,
            }
        )
        save_config(data)


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
            else:
                provider["models"] = [str(item) for item in models if item is not None]
            normalized_providers.append(provider)
        data["providers"] = normalized_providers

    groups = data.get("groups", {})
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
                }
            )
    return providers_list


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


def add_provider(name: str, base_url: str, api_key: str, description: str, models: List[str]) -> None:
    normalized_models = [str(model).strip() for model in models if str(model).strip()]
    if not normalized_models:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one model is required for a provider")
    with config_lock:
        data = config_data
        providers = data.setdefault("providers", [])
        if not isinstance(providers, list):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Invalid providers config")
        if any(provider.get("name") == name for provider in providers if isinstance(provider, dict)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Provider name '{name}' already exists")
        providers.append(
            {
                "name": name,
                "url": base_url,
                "api_key": api_key,
                "description": description,
                "models": normalized_models,
            }
        )
        save_config(data)


def delete_provider(name: str) -> None:
    with config_lock:
        data = config_data
        providers = data.get("providers", [])
        if not isinstance(providers, list):
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Invalid providers config")
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
        if normalized_path.endswith("/v1") or normalized_path.endswith("/openai/v1") or normalized_path.endswith("/v1beta2") or normalized_path.endswith("/v1beta3") or normalized_path == "":
            normalized_path = normalized_path + "/chat/completions"
    new_parsed = parsed._replace(path=normalized_path)
    return urlunparse(new_parsed)


def build_provider_headers(endpoint: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream, application/json",
        "User-Agent": "llm-proxy/1.0",
    }
    api_key = endpoint.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def stream_provider_response(response: httpx.Response):
    async for chunk in response.aiter_bytes():
        if chunk:
            yield chunk

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Proxy deluje!"}

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, background_tasks: BackgroundTasks, api_key_record: sqlite3.Row = Depends(validate_api_key)) -> StreamingResponse:
    payload = await request.json()
    group_name = payload.get("model")
    if not group_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing model group in request payload")
    endpoints = resolve_group(group_name)
    api_key_value = api_key_record["key"]
    started_at = datetime.utcnow().isoformat()
    last_error: Optional[str] = None
    for endpoint in endpoints:
        provider_name = endpoint.get("name", "unknown")
        provider_model = endpoint.get("model")
        provider_payload = dict(payload)
        provider_payload["model"] = provider_model
        headers = build_provider_headers(endpoint)
        target_url = resolve_endpoint_url(endpoint)
        try:
            async with http_client.stream("POST", target_url, json=provider_payload, headers=headers, timeout=30.0) as response:
                if response.status_code == 200:
                    async def proxy_stream() -> Any:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    ended_at = datetime.utcnow().isoformat()
                    background_tasks.add_task(
                        insert_log,
                        api_key_value,
                        group_name,
                        provider_name,
                        provider_model,
                        response.status_code,
                        started_at,
                        ended_at,
                        None,
                    )
                    return StreamingResponse(proxy_stream(), status_code=200, media_type=response.headers.get("content-type", "application/json"))
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    last_error = f"{provider_name} returned {response.status_code}"
                    continue
                content = await response.aread()
                error_msg = content.decode("utf-8", errors="replace")
                ended_at = datetime.utcnow().isoformat()
                background_tasks.add_task(
                    insert_log,
                    api_key_value,
                    group_name,
                    provider_name,
                    provider_model,
                    response.status_code,
                    started_at,
                    ended_at,
                    error_msg,
                )
                return Response(content, status_code=response.status_code, media_type=response.headers.get("content-type", "application/json"))
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError) as exc:
            last_error = f"{provider_name} failed: {exc}"
            continue
        except Exception as exc:
            last_error = f"{provider_name} unexpected error: {exc}"
            continue
    ended_at = datetime.utcnow().isoformat()
    background_tasks.add_task(
        insert_log,
        api_key_value,
        group_name,
        endpoints[-1].get("name", "unknown"),
        endpoints[-1].get("model", "unknown"),
        502,
        started_at,
        ended_at,
        last_error,
    )
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
def admin_providers(request: Request) -> Any:
    return templates.TemplateResponse("providers.html", {"request": request, "providers": get_providers()})


@app.post("/admin/providers", dependencies=[Depends(verify_admin)])
async def admin_providers_add(
    name: str = Form(...),
    base_url: str = Form(...),
    api_key: str = Form(""),
    description: str = Form(""),
    models_value: str = Form(""),
) -> RedirectResponse:
    add_provider(name=name.strip(), base_url=base_url.strip(), api_key=api_key.strip(), description=description.strip(), models=[model.strip() for model in models_value.split(",") if model.strip()])
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
