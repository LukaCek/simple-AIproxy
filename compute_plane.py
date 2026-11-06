"""CPU compute nodes control plane. Isolated from legacy background-job workers."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response, StreamingResponse

import main_impl as core
from compute_catalog import CATALOG

router = APIRouter()
_connections: dict[str, "NodeConnection"] = {}
_registry_lock = asyncio.Lock()
_installed = False


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(str(core.DB_PATH), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    return db


def _setup() -> None:
    with _db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS ComputeEnrollments (
            token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS ComputeNodes (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, secret_hash TEXT NOT NULL,
            allow_private INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
            hardware_json TEXT NOT NULL DEFAULT '{}', models_json TEXT NOT NULL DEFAULT '{}',
            last_seen INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ComputeOperations (
            id TEXT PRIMARY KEY, node_id TEXT NOT NULL, action TEXT NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        """)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _record(node_id: str) -> dict[str, Any] | None:
    _setup()
    with _db() as db:
        row = db.execute("SELECT * FROM ComputeNodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def _public_node(row: dict[str, Any]) -> dict[str, Any]:
    try:
        hardware = json.loads(row["hardware_json"])
        models = json.loads(row["models_json"])
    except (TypeError, ValueError):
        hardware, models = {}, {}
    return {
        "id": row["id"], "name": row["name"], "online": row["id"] in _connections,
        "allow_private": bool(row["allow_private"]), "revoked": bool(row["revoked"]),
        "hardware": hardware, "models": models, "last_seen": row["last_seen"],
    }


@dataclass
class NodeConnection:
    ws: WebSocket
    node_id: str
    pending: dict[str, asyncio.Queue] = field(default_factory=dict)
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.write_lock:
            await self.ws.send_json(message)


async def _dispatch(node_id: str, message: dict[str, Any], *, timeout: float = 900.0):
    conn = _connections.get(node_id)
    if conn is None:
        raise RuntimeError("Node is offline")
    request_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    conn.pending[request_id] = queue
    message = {**message, "id": request_id}
    try:
        await conn.send(message)
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Worker timed out") from exc
            if item.get("type") == "error":
                raise RuntimeError(str(item.get("error", "Worker error"))[:500])
            if item.get("type") in ("result", "end"):
                return item
    finally:
        conn.pending.pop(request_id, None)


async def _stream(node_id: str, model: str, payload: dict[str, Any]):
    conn = _connections.get(node_id)
    if conn is None:
        raise RuntimeError("Node is offline")
    request_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    conn.pending[request_id] = queue
    try:
        await conn.send({"type": "run", "id": request_id, "model": model, "payload": payload})
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=3600)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Inference timed out") from exc
            if item.get("type") == "chunk":
                data = base64.b64decode(item["body"], validate=True)
                if len(data) > 65536:
                    raise RuntimeError("Oversized worker chunk")
                yield data
            elif item.get("type") == "end":
                if int(item.get("status", 200)) != 200:
                    raise RuntimeError("Inference failed")
                return
            elif item.get("type") == "error":
                raise RuntimeError(str(item.get("error", "Worker error"))[:500])
    finally:
        conn.pending.pop(request_id, None)
        if _connections.get(node_id) is conn:
            try:
                await conn.send({"type": "cancel", "id": request_id})
            except Exception:
                pass


def endpoints(model: str) -> list[dict[str, Any]]:
    """Dynamic provider candidates; never infer private-data consent."""
    if not model.startswith("local/"):
        return []
    model_id = model.partition("/")[2]
    if model_id not in CATALOG:
        return []
    _setup()
    with _db() as db:
        rows = db.execute("SELECT * FROM ComputeNodes WHERE revoked=0 ORDER BY allow_private DESC, last_seen DESC").fetchall()
    result = []
    for row in rows:
        node = dict(row)
        if node["id"] not in _connections:
            continue
        try:
            models = json.loads(node["models_json"])
        except (ValueError, TypeError):
            continue
        if models.get("active") != model_id or not models.get("ready"):
            continue
        result.append({
            "name": "compute-" + node["id"][:8], "api_mode": "compute_worker",
            "node_id": node["id"], "model": model_id,
            "allow_private": bool(node["allow_private"]),
        })
    return result


async def forward(endpoint: dict[str, Any], payload: dict[str, Any]) -> Response:
    """Bridge unchanged OpenAI-compatible inference over the node's WSS."""
    visibility = payload.pop("aiproxy_visibility", "private")
    if visibility not in ("private", "public"):
        raise HTTPException(400, "Invalid aiproxy_visibility")
    if visibility == "private" and not endpoint["allow_private"]:
        raise HTTPException(403, "This compute node is not trusted for private requests")
    node_id = endpoint["node_id"]
    if not payload.get("stream"):
        chunks: list[bytes] = []
        total = 0
        async for chunk in _stream(node_id, endpoint["model"], payload):
            total += len(chunk)
            if total > 8 * 1024 * 1024:
                raise HTTPException(413, "Worker response too large")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(502, "Invalid response from worker") from exc
        return Response(content=json.dumps(parsed), media_type="application/json")
    return StreamingResponse(_stream(node_id, endpoint["model"], payload), media_type="text/event-stream")


@router.post("/admin/api/compute/enrollments", dependencies=[Depends(core.verify_admin)])
async def create_enrollment() -> dict[str, Any]:
    _setup()
    token = secrets.token_urlsafe(32)
    expiry = int(time.time()) + 900
    with _db() as db:
        db.execute("INSERT INTO ComputeEnrollments(token_hash, expires) VALUES (?, ?)", (_hash(token), expiry))
    return {"token": token, "expires_at": expiry}


@router.post("/compute/enroll")
async def enroll(request: Request) -> dict[str, str]:
    _setup()
    data = await request.json()
    token = str(data.get("token") or "")
    name = str(data.get("name") or "Compute Worker")[:80].strip() or "Compute Worker"
    if len(token) > 256 or not token:
        raise HTTPException(401, "Invalid enrollment token")
    node_id, secret = uuid.uuid4().hex, secrets.token_urlsafe(48)
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT expires, used FROM ComputeEnrollments WHERE token_hash=?", (_hash(token),)).fetchone()
        if not row or row["used"] or row["expires"] < int(time.time()):
            raise HTTPException(401, "Invalid or expired enrollment token")
        db.execute("UPDATE ComputeEnrollments SET used=1 WHERE token_hash=?", (_hash(token),))
        db.execute("INSERT INTO ComputeNodes(id,name,secret_hash,created_at) VALUES (?,?,?,?)",
                   (node_id, name, _hash(secret), int(time.time())))
    return {"node_id": node_id, "node_secret": secret}


@router.websocket("/compute/ws")
async def compute_ws(ws: WebSocket):
    await ws.accept()
    conn = None
    try:
        data = await asyncio.wait_for(ws.receive_json(), timeout=15)
        if data.get("type") != "auth":
            await ws.close(code=1008)
            return
        node_id = str(data.get("node_id", ""))
        secret = str(data.get("node_secret", ""))
        if len(node_id) != 32 or len(secret) > 256:
            await ws.close(code=1008)
            return
        row = _record(node_id)
        if not row or row["revoked"] or not secrets.compare_digest(row["secret_hash"], _hash(secret)):
            await ws.close(code=1008)
            return
        conn = NodeConnection(ws=ws, node_id=node_id)
        async with _registry_lock:
            old = _connections.get(node_id)
            if old:
                await old.ws.close(code=1012)
            _connections[node_id] = conn
        await conn.send({"type": "auth_ok", "protocol": 1})
        with _db() as db:
            db.execute("UPDATE ComputeNodes SET last_seen=? WHERE id=?", (int(time.time()), node_id))
        while True:
            message = await ws.receive_json()
            if len(json.dumps(message)) > 256 * 1024:
                await ws.close(code=1009)
                break
            kind = message.get("type")
            if kind == "telemetry":
                hardware = message.get("hardware", {})
                models = message.get("models", {})
                if not isinstance(hardware, dict) or not isinstance(models, dict):
                    continue
                with _db() as db:
                    db.execute("UPDATE ComputeNodes SET last_seen=?,hardware_json=?,models_json=? WHERE id=?",
                               (int(time.time()), json.dumps(hardware), json.dumps(models), node_id))
            elif kind in ("chunk", "result", "end", "error"):
                pending = conn.pending.get(str(message.get("id") or ""))
                if pending:
                    await pending.put(message)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception:
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        if conn:
            for queue in conn.pending.values():
                try:
                    queue.put_nowait({"type": "error", "error": "Worker disconnected"})
                except asyncio.QueueFull:
                    pass
            if _connections.get(conn.node_id) is conn:
                _connections.pop(conn.node_id, None)


@router.get("/admin/api/compute/nodes", dependencies=[Depends(core.verify_admin)])
async def list_nodes() -> dict[str, Any]:
    _setup()
    with _db() as db:
        rows = db.execute("SELECT * FROM ComputeNodes ORDER BY created_at DESC").fetchall()
    return {"nodes": [_public_node(dict(row)) for row in rows]}


@router.post("/admin/api/compute/nodes/{node_id}/trust", dependencies=[Depends(core.verify_admin)])
async def set_trust(node_id: str, request: Request):
    data = await request.json()
    if type(data.get("allow_private")) is not bool:
        raise HTTPException(400, "allow_private must be a boolean")
    with _db() as db:
        result = db.execute("UPDATE ComputeNodes SET allow_private=? WHERE id=? AND revoked=0",
                            (int(data["allow_private"]), node_id))
        if not result.rowcount:
            raise HTTPException(404, "Node not found")
    return {"node_id": node_id, "allow_private": data["allow_private"]}


@router.post("/admin/api/compute/nodes/{node_id}/revoke", dependencies=[Depends(core.verify_admin)])
async def revoke_node(node_id: str):
    with _db() as db:
        result = db.execute("UPDATE ComputeNodes SET revoked=1,allow_private=0 WHERE id=?", (node_id,))
        if not result.rowcount:
            raise HTTPException(404, "Node not found")
    conn = _connections.get(node_id)
    if conn:
        await conn.ws.close(code=1008)
    return {"revoked": True}


@router.get("/admin/api/compute/catalog", dependencies=[Depends(core.verify_admin)])
async def catalog() -> dict[str, Any]:
    return {"models": CATALOG}


@router.post("/admin/api/compute/nodes/{node_id}/operations", dependencies=[Depends(core.verify_admin)])
async def start_operation(node_id: str, request: Request):
    data = await request.json()
    action = str(data.get("action") or "")
    model = str(data.get("model") or "")
    if action not in ("install", "load", "unload", "delete") or model not in CATALOG:
        raise HTTPException(400, "Invalid model or operation")
    if node_id not in _connections:
        raise HTTPException(409, "Node is offline")
    operation = uuid.uuid4().hex
    now = int(time.time())
    with _db() as db:
        db.execute("INSERT INTO ComputeOperations(id,node_id,action,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                   (operation, node_id, action, "pending", now, now))

    async def run():
        try:
            await _dispatch(node_id, {"type": "command", "action": action, "model": model}, timeout=7200)
            state, detail = "succeeded", ""
        except Exception as exc:
            state, detail = "failed", str(exc)[:500]
        with _db() as db:
            db.execute("UPDATE ComputeOperations SET status=?,detail=?,updated_at=? WHERE id=?",
                       (state, detail, int(time.time()), operation))

    asyncio.create_task(run())
    return {"id": operation, "status": "pending"}


@router.get("/admin/api/compute/operations/{op_id}", dependencies=[Depends(core.verify_admin)])
async def operation_status(op_id: str):
    _setup()
    with _db() as db:
        row = db.execute("SELECT * FROM ComputeOperations WHERE id=?", (op_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Operation not found")
    return dict(row)


@router.get("/admin/compute-nodes", response_class=HTMLResponse, dependencies=[Depends(core.verify_admin)])
async def admin_page(request: Request):
    _setup()
    with _db() as db:
        rows = db.execute("SELECT * FROM ComputeNodes ORDER BY created_at DESC").fetchall()
    return core.templates.TemplateResponse(
        request, "compute_nodes.html",
        {"nodes": [_public_node(dict(r)) for r in rows], "catalog": CATALOG},
    )


def install(app):
    global _installed
    if not _installed:
        _setup()
        app.include_router(router)
        _installed = True
