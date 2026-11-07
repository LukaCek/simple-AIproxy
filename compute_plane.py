"""CPU compute nodes control plane. Isolated from legacy background-job workers."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import base64
import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import main_impl as core
from compute_catalog import CATALOG
from compute_placement import models_for_node
from compute_scheduler import auto_candidates as select_auto_candidates
from compute_setup import compose_text, env_text, validate_setup

router = APIRouter()


def require_admin_write(request: Request, _admin=Depends(core.verify_admin)) -> None:
    # Blocks cross-origin forms using cached HTTP Basic credentials.
    if request.headers.get("x-simpleai-admin-action") != "1":
        raise HTTPException(403, "Missing admin action header")
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlsplit
        if urlsplit(origin).netloc != request.url.netloc:
            raise HTTPException(403, "Origin mismatch")

_connections: dict[str, "NodeConnection"] = {}
_registry_lock = asyncio.Lock()
# One proxy event-loop process is the supported coordinator topology for v1.
# Reserve before dispatch: heartbeat telemetry is too stale to prevent races.
_reservation_lock = threading.Lock()
_reservations: dict[str, str] = {}
_installed = False


async def _reserve(node_id: str, timeout: float = 2.0) -> str | None:
    token = uuid.uuid4().hex
    deadline = time.monotonic() + timeout
    while True:
        with _reservation_lock:
            if node_id not in _reservations:
                _reservations[node_id] = token
                return token
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.05)


def _release(node_id: str, token: str) -> None:
    with _reservation_lock:
        if _reservations.get(node_id) == token:
            _reservations.pop(node_id, None)



@contextmanager
def _db():
    db = sqlite3.connect(str(core.DB_PATH), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=15000")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
        CREATE TABLE IF NOT EXISTS ComputePolicy (
            id INTEGER PRIMARY KEY CHECK(id=1),
            revision INTEGER NOT NULL,
            models_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ComputeSettings (
            id INTEGER PRIMARY KEY CHECK(id=1),
            placement_mode TEXT NOT NULL DEFAULT 'manual'
        );
        """)
        db.execute("INSERT OR IGNORE INTO ComputePolicy(id,revision,models_json,updated_at) VALUES(1,1,'[]',?)", (int(time.time()),))
        db.execute("INSERT OR IGNORE INTO ComputeSettings(id,placement_mode) VALUES(1,'manual')")


def _model_policy() -> dict[str, Any]:
    """One global admin-maintained model target list for every remote worker."""
    _setup()
    with _db() as db:
        row = db.execute("SELECT revision,models_json FROM ComputePolicy WHERE id=1").fetchone()
    selected = json.loads(row["models_json"]) if row else []
    if not isinstance(selected, list):
        selected = []
    return {"type": "policy", "revision": int(row["revision"]) if row else 1,
            "models": [mid for mid in selected if isinstance(mid, str) and mid in CATALOG]}


def _placement_mode() -> str:
    _setup()
    with _db() as db:
        row = db.execute("SELECT placement_mode FROM ComputeSettings WHERE id=1").fetchone()
    return row["placement_mode"] if row and row["placement_mode"] in ("manual", "auto") else "manual"


def _node_policy(node_id: str, global_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = dict(global_policy if global_policy is not None else _model_policy())
    if _placement_mode() != "auto":
        return policy
    row = _record(node_id)
    if not row:
        return policy
    try:
        hardware = json.loads(row["hardware_json"])
        state = json.loads(row["models_json"])
    except (TypeError, ValueError):
        return policy
    if isinstance(hardware, dict) and isinstance(state, dict):
        policy["models"] = models_for_node(policy["models"], CATALOG, hardware, state)
    return policy


async def _send_node_policy(conn: "NodeConnection") -> None:
    policy = _node_policy(conn.node_id)
    signature = (policy["revision"], tuple(policy["models"]))
    if signature == conn.last_policy_signature:
        return
    if policy["revision"] <= 1 and not policy["models"]:
        return
    await conn.send(policy)
    conn.last_policy_signature = signature


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _record(node_id: str) -> dict[str, Any] | None:
    _setup()
    with _db() as db:
        row = db.execute("SELECT * FROM ComputeNodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def _nonnegative_int(value: Any) -> int:
    """Treat telemetry from untrusted machines as untrusted input."""
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), 2**63 - 1))
    except (TypeError, ValueError, OverflowError):
        return 0


def _public_node(row: dict[str, Any]) -> dict[str, Any]:
    try:
        hardware = json.loads(row["hardware_json"])
        models = json.loads(row["models_json"])
    except (TypeError, ValueError):
        hardware, models = {}, {}
    if not isinstance(hardware, dict):
        hardware = {}
    if not isinstance(models, dict):
        models = {}
    hardware = dict(hardware)
    for key in ("cpus", "memory_limit_bytes", "memory_available_bytes", "disk_free_bytes", "disk_total_bytes"):
        hardware[key] = _nonnegative_int(hardware.get(key))
    models = dict(models)
    installed = models.get("installed")
    models["installed"] = [mid for mid in installed if isinstance(mid, str) and mid in CATALOG] if isinstance(installed, list) else []
    capabilities = models.get("capabilities")
    safe_capabilities = {}
    if isinstance(capabilities, dict):
        for mid, state in capabilities.items():
            if mid not in CATALOG or not isinstance(state, dict):
                continue
            status_name = state.get("status")
            if status_name not in ("untested", "testing", "supported", "unsupported", "error"):
                status_name = "untested"
            safe_capabilities[mid] = {
                "status": status_name, "reason": str(state.get("reason") or "")[:240],
                "tested_at": _nonnegative_int(state.get("tested_at")),
                "test_seconds": max(0.0, min(float(state.get("test_seconds") or 0), 86400.0))
                    if isinstance(state.get("test_seconds"), (int, float)) else 0.0,
            }
            benchmark = state.get("benchmark", {})
            if isinstance(benchmark, dict):
                safe_bench = {}
                for name in ("prompt_tps", "generation_tps", "load_seconds", "wall_seconds"):
                    v = benchmark.get(name)
                    if (isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) and 0 <= v <= 1_000_000):
                        safe_bench[name] = round(float(v), 3)
                safe_capabilities[mid]["benchmark"] = safe_bench
    models["capabilities"] = safe_capabilities
    models["busy"] = bool(models.get("busy", False))
    models["running_tasks"] = min(1, _nonnegative_int(models.get("running_tasks")))
    models["disk_used_bytes"] = _nonnegative_int(models.get("disk_used_bytes"))
    models["disk_limit_bytes"] = _nonnegative_int(models.get("disk_limit_bytes"))
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
    last_policy_signature: tuple[int, tuple[str, ...]] | None = None

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


async def _stream(node_id: str, model: str, payload: dict[str, Any], *, visibility: str = "private"):
    # StreamingResponse bodies run after forward() returns. Check again before
    # sending any prompt, not only when preparing the HTTP response.
    current = _record(node_id)
    if not current or current["revoked"]:
        raise HTTPException(403, "Compute node has been revoked or removed")
    policy = _model_policy()
    if policy["revision"] > 1 and model not in policy["models"]:
        raise HTTPException(409, "Model disabled by current compute policy")
    if visibility == "private" and not bool(current["allow_private"]):
        raise HTTPException(403, "This compute node is not trusted for private requests")
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


def model_recommendations(row: dict[str, Any]) -> dict[str, Any]:
    """Conservative suitability hints, not a promise that inference will succeed."""
    try:
        hw = json.loads(row["hardware_json"])
        state = json.loads(row["models_json"])
    except (TypeError, ValueError):
        hw, state = {}, {}
    if not isinstance(hw, dict):
        hw = {}
    if not isinstance(state, dict):
        state = {}
    ram = min(_nonnegative_int(hw.get("memory_limit_bytes")),
              _nonnegative_int(hw.get("memory_available_bytes")))
    disk = _nonnegative_int(hw.get("disk_free_bytes"))
    installed = state.get("installed") if isinstance(state.get("installed"), list) else []
    recommendations = {}
    for mid, model in CATALOG.items():
        reserve = min(2 * 1024**3, max(512 * 1024**2, ram // 8))
        required_ram = int(model["size"] * 1.25) + reserve
        required_disk = int(model["size"] * 1.15)
        if not ram or not disk:
            grade, reason = "unknown", "Waiting for hardware telemetry"
        elif ram < required_ram:
            grade, reason = "too_large", "Insufficient available RAM"
        elif mid not in installed and disk < required_disk:
            grade, reason = "too_large", "Insufficient free disk"
        elif ram < required_ram * 1.3:
            grade, reason = "possible", "Near memory budget; measure before production"
        else:
            grade, reason = "recommended", "Fits conservative CPU memory estimate"
        recommendations[mid] = {
            "grade": grade, "reason": reason, "estimated_ram_bytes": required_ram,
            "estimated_download_bytes": required_disk, "installed": mid in installed,
        }
    return recommendations


def endpoints(model: str) -> list[dict[str, Any]]:
    """Dynamic provider candidates; never infer private-data consent."""
    if not model.startswith("local/"):
        return []
    model_id = model.partition("/")[2]
    if model_id not in CATALOG:
        return []
    policy = _model_policy()
    placement_auto = _placement_mode() == "auto"
    with _db() as db:
        rows = db.execute("SELECT * FROM ComputeNodes WHERE revoked=0 ORDER BY allow_private DESC, last_seen DESC").fetchall()
    selected = policy["models"]
    managed = policy["revision"] > 1
    result = []
    for row in rows:
        node = dict(row)
        if node["id"] not in _connections:
            continue
        try:
            models = json.loads(node["models_json"])
        except (ValueError, TypeError):
            continue
        current_ready = models.get("active") == model_id and bool(models.get("ready"))
        test_state = models.get("capabilities") if isinstance(models.get("capabilities"), dict) else {}
        verified = isinstance(test_state.get(model_id), dict) and test_state[model_id].get("status") == "supported"
        # Managed routing exposes *tested* models, even when another GGUF
        # currently occupies the worker's single llama.cpp runtime slot.
        if managed:
            if model_id not in selected or not verified:
                continue
            if placement_auto:
                try:
                    hardware = json.loads(node["hardware_json"])
                except (TypeError, ValueError):
                    hardware = {}
                assigned = models_for_node(selected, CATALOG, hardware, models) if isinstance(hardware, dict) else selected
                if model_id not in assigned:
                    continue
        elif not current_ready:
            continue
        public = _public_node(node)
        benchmark = public["models"].get("capabilities", {}).get(model_id, {}).get("benchmark", {})
        with _reservation_lock:
            reserved = node["id"] in _reservations
        result.append({
            "name": "compute-" + node["id"][:8], "api_mode": "compute_worker",
            "node_id": node["id"], "model": model_id,
            "allow_private": bool(node["allow_private"]),
            "compute_metrics": {
                "warm": current_ready,
                "busy": reserved or public["models"].get("busy", False),
                "benchmark": benchmark,
            },
        })
    return result


def automatic_endpoints(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve local/auto to ONE exact catalog model, not a mixed-model fallback."""
    policy = _model_policy()
    approved = policy["models"] if policy["revision"] > 1 else list(CATALOG)
    candidates = [candidate for mid in approved for candidate in endpoints("local/" + mid)]
    return select_auto_candidates(candidates, payload, CATALOG)


def prepare_worker_thinking(payload: dict[str, Any]) -> dict[str, Any]:
    """Configure per-request thinking with a bounded reasoning token budget.

    The worker defaults to reasoning ON. llama.cpp accepts enable_thinking via
    chat_template_kwargs, and reasoning_budget_tokens at request level. The
    budget forces the thinking block to end early, reserving tokens for visible
    content instead of exhausting max_tokens entirely on reasoning.
    """
    body = dict(payload)
    body.pop("aiproxy_compute_tier", None)
    explicit_think = "think" in body
    think = body.pop("think") if explicit_think else None
    if explicit_think and type(think) is not bool:
        raise HTTPException(400, "think must be true or false")

    original_kwargs = body.get("chat_template_kwargs", {})
    if not isinstance(original_kwargs, dict):
        raise HTTPException(400, "chat_template_kwargs must be an object")
    template_think = original_kwargs.get("enable_thinking", True)
    if type(template_think) is not bool:
        raise HTTPException(400, "chat_template_kwargs.enable_thinking must be boolean")
    if explicit_think and think and body.get("reasoning_effort") == "none":
        raise HTTPException(400, "think=true conflicts with reasoning_effort=none")

    # Explicit think wins over template knobs; otherwise retain the worker's
    # default ON setting (or an explicit OpenAI reasoning_effort=none).
    if explicit_think:
        body["chat_template_kwargs"] = {**original_kwargs, "enable_thinking": think}
    else:
        think = template_think
    if body.get("reasoning_effort") == "none":
        think = False

    if not think:
        return body

    # Respect every known completion cap; do not inflate a caller's limit.
    caps = []
    for field in ("max_tokens", "max_completion_tokens", "n_predict"):
        raw = body.get(field)
        if raw is not None:
            if type(raw) is not int or raw <= 0:
                raise HTTPException(400, f"{field} must be a positive integer")
            caps.append(raw)
    completion_cap = min(caps) if caps else None

    provided = [name for name in ("reasoning_budget_tokens", "thinking_budget_tokens")
                if name in body]
    if len(provided) > 1:
        raise HTTPException(400, "Specify one reasoning budget field")
    if provided:
        value = body[provided[0]]
        if type(value) is not int or value < 0:
            raise HTTPException(400, "reasoning budget must be a nonnegative integer")
        if completion_cap is not None and value >= completion_cap:
            raise HTTPException(400, "reasoning budget must be below max_tokens")
        # Always use llama.cpp's canonical field.
        body.pop(provided[0])
        body["reasoning_budget_tokens"] = value
    else:
        # For 512 output tokens, budget 128 for thought and leave 384 for answer.
        # Without a stated cap, still avoid unbounded CPU thinking.
        body["reasoning_budget_tokens"] = (
            min(256, completion_cap // 4) if completion_cap is not None else 192
        )
    return body

async def forward(endpoint: dict[str, Any], payload: dict[str, Any]) -> Response:
    """Bridge OpenAI-compatible inference over the node's WSS."""
    visibility = payload.pop("aiproxy_visibility", "private")
    if visibility not in ("private", "public"):
        raise HTTPException(400, "Invalid aiproxy_visibility")
    payload = prepare_worker_thinking(payload)
    # Endpoint candidates are snapshots: re-check DB consent immediately
    # before forwarding so an admin trust change/revocation takes effect.
    node_id = endpoint["node_id"]
    current = _record(node_id)
    if not current or current["revoked"]:
        raise HTTPException(403, "Compute node has been revoked or removed")
    if visibility == "private" and not bool(current["allow_private"]):
        raise HTTPException(403, "This compute node is not trusted for private requests")
    token = await _reserve(node_id)
    if token is None:
        raise HTTPException(503, "Compute worker busy or disconnected")
    if not payload.get("stream"):
        try:
            chunks: list[bytes] = []
            total = 0
            async for chunk in _stream(node_id, endpoint["model"], payload, visibility=visibility):
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
        finally:
            _release(node_id, token)

    async def reserved_stream():
        try:
            async for chunk in _stream(node_id, endpoint["model"], payload, visibility=visibility):
                yield chunk
        finally:
            # Streaming owns the slot until the final byte or client disconnect.
            _release(node_id, token)

    return StreamingResponse(reserved_stream(), media_type="text/event-stream")


@router.get("/admin/compute-setup", response_class=HTMLResponse, dependencies=[Depends(core.verify_admin)])
async def setup_page(request: Request):
    return core.templates.TemplateResponse(request=request, name="compute_setup.html", context={})


@router.post("/admin/api/compute/setup", dependencies=[Depends(require_admin_write)])
async def generate_worker_setup(request: Request) -> JSONResponse:
    """Issue short-lived credentials only on explicit admin action."""
    try:
        settings = validate_setup(await request.json())
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    enrollment = await create_enrollment()
    return JSONResponse({
        "compose": compose_text(settings),
        "env": env_text(settings, enrollment["token"]),
        "expires_at": enrollment["expires_at"],
        "instructions": [
            "Save docker-compose.yml and .env in the same empty folder.",
            "Protect .env with chmod 600 .env and do not commit or share it.",
            "Run: docker compose config --quiet && docker compose pull && docker compose up -d",
            "Confirm the node is Online in SimpleAI Proxy.",
            "After enrollment succeeds, remove the one-time token from .env.",
        ],
    }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/admin/api/compute/enrollments", dependencies=[Depends(require_admin_write)])
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
        if data.get("type") != "auth" or data.get("protocol") != 1:
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
        # A node's policy may differ from the globally approved model list.
        await _send_node_policy(conn)
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
                await _send_node_policy(conn)
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


@router.get("/admin/api/compute/nodes/{node_id}/recommendations", dependencies=[Depends(core.verify_admin)])
async def node_recommendations(node_id: str):
    node = _record(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    return {"node_id": node_id, "models": model_recommendations(node)}


@router.post("/admin/api/compute/nodes/{node_id}/trust", dependencies=[Depends(require_admin_write)])
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


@router.post("/admin/api/compute/nodes/{node_id}/revoke", dependencies=[Depends(require_admin_write)])
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


@router.get("/admin/api/compute/model-policy", dependencies=[Depends(core.verify_admin)])
async def read_model_policy() -> dict[str, Any]:
    return _model_policy()


@router.post("/admin/api/compute/model-policy", dependencies=[Depends(require_admin_write)])
async def change_model_policy(request: Request) -> dict[str, Any]:
    data = await request.json()
    selected = data.get("models") if isinstance(data, dict) else None
    if (not isinstance(selected, list) or len(selected) > len(CATALOG)
            or any(not isinstance(mid, str) or mid not in CATALOG for mid in selected)
            or len(set(selected)) != len(selected)):
        raise HTTPException(400, "models must be unique entries from the approved catalog")
    # Every save increases revision, allowing an explicit re-test without
    # shipping an untrusted URL or arbitrary command to remote computers.
    _setup()
    with _db() as db:
        row = db.execute("SELECT revision FROM ComputePolicy WHERE id=1").fetchone()
        version = int(row["revision"]) + 1
        db.execute("UPDATE ComputePolicy SET revision=?,models_json=?,updated_at=? WHERE id=1",
                   (version, json.dumps(selected), int(time.time())))
    policy = {"type": "policy", "revision": version, "models": selected}
    for conn in list(_connections.values()):
        try:
            await _send_node_policy(conn)
        except Exception:
            pass  # The worker receives the latest policy on its next WSS reconnect.
    return policy


@router.get("/admin/api/compute/placement", dependencies=[Depends(core.verify_admin)])
async def placement_settings() -> dict[str, str]:
    return {"mode": _placement_mode()}


@router.post("/admin/api/compute/placement", dependencies=[Depends(require_admin_write)])
async def update_placement(request: Request) -> dict[str, str]:
    data = await request.json()
    mode = data.get("mode") if isinstance(data, dict) else None
    if mode not in ("manual", "auto"):
        raise HTTPException(400, "mode must be manual or auto")
    _setup()
    with _db() as db:
        db.execute("UPDATE ComputeSettings SET placement_mode=? WHERE id=1", (mode,))
    for conn in list(_connections.values()):
        try:
            await _send_node_policy(conn)
        except Exception:
            pass
    return {"mode": mode}


@router.post("/admin/api/compute/nodes/{node_id}/operations", dependencies=[Depends(require_admin_write)])
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
        {"nodes": [_public_node(dict(r)) for r in rows], "catalog": CATALOG,
         "policy": _model_policy(), "placement_mode": _placement_mode(),
         "recommendations": {r["id"]: model_recommendations(dict(r)) for r in rows}},
    )


def install(app):
    global _installed
    if not _installed:
        _setup()
        app.include_router(router)
        _installed = True
