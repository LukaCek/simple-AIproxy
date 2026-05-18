import asyncio
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yml"
DB_PATH = BASE_DIR / "app.db"

app = FastAPI(title="LLM API Gateway")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
config_lock = threading.Lock()
config_data: Dict[str, Any] = {}
http_client: Optional[httpx.AsyncClient] = None
watchdog_observer: Optional[Observer] = None


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
        return data


def save_config(data: Dict[str, Any]) -> None:
    with config_lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
        global config_data
        config_data = data


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


def resolve_group(group_name: str) -> List[Dict[str, Any]]:
    with config_lock:
        groups = config_data.get("groups", {})
        group = groups.get(group_name)
        if not group or not isinstance(group, dict):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Model group '{group_name}' not configured")
        endpoints = group.get("endpoints", [])
    sorted_endpoints = sorted(endpoints, key=lambda item: item.get("priority", 1000))
    if not sorted_endpoints:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"No endpoints configured for group '{group_name}'")
    return sorted_endpoints


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
        try:
            async with http_client.stream("POST", endpoint["url"], json=provider_payload, headers=headers, timeout=30.0) as response:
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


@app.get("/admin/keys", response_class=HTMLResponse)
def admin_keys(request: Request) -> Any:
    return templates.TemplateResponse("keys.html", {"request": request, "api_keys": list_api_keys()})


@app.post("/admin/keys/new")
async def admin_keys_new(name: str = Form(...)) -> RedirectResponse:
    create_api_key(name)
    return RedirectResponse(url="/admin/keys", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/logs", response_class=HTMLResponse)
def admin_logs(request: Request) -> Any:
    return templates.TemplateResponse("logs.html", {"request": request, "logs": list_logs()})


@app.get("/admin/config", response_class=HTMLResponse)
def admin_config(request: Request, message: Optional[str] = None) -> Any:
    with config_lock:
        yaml_text = yaml.safe_dump(config_data, sort_keys=False)
    return templates.TemplateResponse(
        "config.html",
        {"request": request, "yaml_text": yaml_text, "message": message},
    )


@app.post("/admin/config/save")
async def admin_config_save(yaml_text: str = Form(...)) -> RedirectResponse:
    try:
        parsed = yaml.safe_load(yaml_text)
        if parsed is None:
            parsed = {}
        save_config(parsed)
        return RedirectResponse(url="/admin/config", status_code=status.HTTP_303_SEE_OTHER)
    except yaml.YAMLError as exc:
        return HTMLResponse(content=f"Invalid YAML: {exc}", status_code=400)


@app.post("/admin/config/update")
async def api_config_update(payload: Dict[str, Any]) -> Dict[str, Any]:
    save_config(payload)
    return {"status": "ok", "message": "Configuration updated."}


@app.get("/admin", response_class=HTMLResponse)
def admin_root(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/admin/keys")
