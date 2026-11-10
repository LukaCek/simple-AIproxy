"""Authenticated stateless Streamable HTTP MCP endpoint for SimpleAI Proxy.

Implements MCP JSON-RPC over POST with JSON responses; no new port or
background daemon. Token hashes only, scoped read/operate, revocable in admin.
No generic shell/file/config editor, Docker socket or bearer key exposure.
Compatible with MCP 2025-03-26, 2025-11-25 and stateless 2026-07-28 clients.
"""
from __future__ import annotations

from collections import deque
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import simpleai_mcp_logging as safe_logs
import simpleai_mcp_oauth as oauth
import simpleai_mcp_tools as toolset

_router = APIRouter(include_in_schema=False)
_core = None
_app = None
_installed = False
_SUPPORTED = ("2026-07-28", "2025-11-25", "2025-03-26")
_RATE_LOCK = Lock()
_RATE: dict[str, deque[float]] = {}
_MAX_CALLS_PER_MINUTE = 80
_MAX_RPC_BYTES = 65536


def _db():
    if _core is None:
        raise RuntimeError("MCP not installed")
    return _core.get_db_connection()


def _setup() -> None:
    with _db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS MCPAccessTokens (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL CHECK(scope IN ('read','operate')),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            last_used INTEGER NOT NULL DEFAULT 0,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_expires ON MCPAccessTokens(expires_at);
        """)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _origin_ok(req: Request) -> bool:
    origin = req.headers.get("origin", "")
    if not origin:
        return True
    # Browser origins are never automatically trusted. ChatGPT's server-to-
    # server POST normally has no Origin; optional extra origins need env setup.
    allowed = {"https://aiproxy.cekluka.com"}
    allowed.update(v.strip() for v in os.getenv("SIMPLEAI_MCP_ALLOWED_ORIGINS", "").split(",") if v.strip())
    return origin in allowed


def _require_admin_action(req: Request) -> None:
    if not _origin_ok(req):
        raise HTTPException(403, "Origin forbidden")
    if req.headers.get("X-SimpleAI-Admin-Action") != "1":
        raise HTTPException(403, "Missing explicit admin write intent")


def _rate_check(key: str) -> None:
    now = time.monotonic()
    with _RATE_LOCK:
        q = _RATE.setdefault(key, deque())
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= _MAX_CALLS_PER_MINUTE:
            raise HTTPException(429, "MCP request rate exceeded")
        q.append(now)
        # Avoid unbounded per-key state on long-running nodes.
        if len(_RATE) > 500:
            for k in list(_RATE)[:200]:
                if not _RATE[k] or _RATE[k][-1] < now - 120:
                    _RATE.pop(k, None)


def _authenticate(req: Request) -> dict:
    auth = req.headers.get("authorization", "")
    def unauthorized():
        raise HTTPException(401, "MCP OAuth authorization or an existing MCP token is required",
                            headers={"WWW-Authenticate": oauth.challenge_header(),
                                     "Cache-Control": "no-store"})
    if not auth.startswith("Bearer ") or len(auth) > 256:
        unauthorized()
    value = auth[7:].strip()
    if value.startswith("simpleai_oauth_"):
        principal = oauth.validate_access(value)
        if principal is None:
            unauthorized()
        _rate_check(principal["id"])
        return principal
    if not re.fullmatch(r"simpleai_mcp_[A-Za-z0-9_-]{32,128}", value):
        unauthorized()
    _setup()
    digest = _hash(value)
    with _db() as db:
        token = db.execute("SELECT id,name,scope,expires_at,revoked "
                           "FROM MCPAccessTokens WHERE token_hash=?", (digest,)).fetchone()
        if not token or token["revoked"] or token["expires_at"] < int(time.time()):
            unauthorized()
        db.execute("UPDATE MCPAccessTokens SET last_used=? WHERE id=?",
                   (int(time.time()),token["id"]))
    _rate_check(token["id"])
    return dict(token)


@_router.get("/admin/mcp", response_class=HTMLResponse)
async def admin_mcp_page(req: Request, _: None = Depends(lambda: None)):
    # Basic authentication checked explicitly because this route is registered
    # on an existing FastAPI app, sharing the standard SimpleAI admin identity.
    from fastapi.security import HTTPBasicCredentials
    authorization = req.headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        raise HTTPException(401, "Admin login required",
                            headers={"WWW-Authenticate":"Basic"})
    import base64
    try:
        raw = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
        username,password = raw.split(":",1)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(401, "Invalid Basic authentication",
                            headers={"WWW-Authenticate":"Basic"})
    if not (secrets.compare_digest(username,_core.ADMIN_USERNAME)
            and secrets.compare_digest(password,_core.ADMIN_PASSWORD)):
        raise HTTPException(401, "Invalid credentials",
                            headers={"WWW-Authenticate":"Basic"})
    _setup()
    return _core.templates.TemplateResponse(
        request=req, name="mcp_access.html", context={"mcp_path":"/mcp"})


def _admin_dependency(req: Request):
    """Use the existing Basic-auth admin guard (shared credentials)."""
    from fastapi.security import HTTPBasicCredentials
    import base64
    auth=req.headers.get("authorization","")
    if not auth.startswith("Basic "):
        raise HTTPException(401, "Admin credentials required",
                            headers={"WWW-Authenticate":"Basic"})
    try:
        raw=base64.b64decode(auth[6:],validate=True).decode("utf-8")
        user,password=raw.split(":",1)
    except (UnicodeError,ValueError):
        raise HTTPException(401,"Admin credentials required",
                            headers={"WWW-Authenticate":"Basic"})
    if not (secrets.compare_digest(user,_core.ADMIN_USERNAME)
            and secrets.compare_digest(password,_core.ADMIN_PASSWORD)):
        raise HTTPException(401,"Invalid admin credentials",
                            headers={"WWW-Authenticate":"Basic"})


@_router.get("/admin/api/mcp/tokens", dependencies=[Depends(_admin_dependency)])
async def admin_list_tokens():
    _setup()
    with _db() as db:
        rows=db.execute("SELECT id,name,scope,created_at,expires_at,last_used,revoked "
                        "FROM MCPAccessTokens ORDER BY created_at DESC LIMIT 100").fetchall()
    return {"tokens":[dict(r) for r in rows]}


@_router.post("/admin/api/mcp/tokens", dependencies=[Depends(_admin_dependency)])
async def admin_issue_token(req: Request):
    _require_admin_action(req)
    data=await req.json()
    if not isinstance(data,dict):
        raise HTTPException(400,"Invalid token request")
    name=data.get("name","ChatGPT MCP")
    scope=data.get("scope","read")
    lifetime=data.get("expires_days",30)
    if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80 or not re.fullmatch(r"[\w .()\-]{1,80}",name):
        raise HTTPException(400,"Invalid token label")
    if scope not in ("read","operate") or type(lifetime) is not int or not 1 <= lifetime <= 90:
        raise HTTPException(400,"Invalid scope or expiry")
    token="simpleai_mcp_"+secrets.token_urlsafe(48)
    now=int(time.time())
    token_id=uuid.uuid4().hex
    _setup()
    with _db() as db:
        db.execute("INSERT INTO MCPAccessTokens(id,name,token_hash,scope,created_at,expires_at) "
                   "VALUES(?,?,?,?,?,?)", (token_id,name.strip(),_hash(token),scope,now,now+lifetime*86400))
    # Token returned once, never persisted in plaintext and not logged.
    return JSONResponse({"id":token_id,"token":token,"scope":scope,
                         "expires_at":now+lifetime*86400},
                        headers={"Cache-Control":"no-store"})


@_router.post("/admin/api/mcp/tokens/{token_id}/revoke", dependencies=[Depends(_admin_dependency)])
async def admin_revoke_token(req: Request, token_id: str):
    _require_admin_action(req)
    if not re.fullmatch(r"[a-f0-9]{32}",token_id):
        raise HTTPException(400,"Invalid token ID")
    _setup()
    with _db() as db:
        update=db.execute("UPDATE MCPAccessTokens SET revoked=1 WHERE id=?", (token_id,))
    if not update.rowcount:
        raise HTTPException(404,"Token not found")
    return {"revoked":True,"id":token_id}


def _rpc_error(id_, code: int, message: str) -> dict:
    return {"jsonrpc":"2.0","id":id_,"error":{"code":code,"message":message}}


def _rpc_response(id_,result: dict) -> dict:
    return {"jsonrpc":"2.0","id":id_,"result":result}


def _response(result: dict, code: int = 200) -> JSONResponse:
    return JSONResponse(result,status_code=code,
                        headers={"Cache-Control":"no-store",
                                 "X-Content-Type-Options":"nosniff"})


@_router.post("/mcp")
async def mcp(req: Request):
    if not _origin_ok(req):
        raise HTTPException(403,"Untrusted MCP Origin")
    principal=_authenticate(req)
    typ=req.headers.get("content-type","").split(";")[0].strip().lower()
    if typ!="application/json":
        return _response(_rpc_error(None,-32600,"Content-Type must be application/json"),415)
    accepted=req.headers.get("accept","")
    if "application/json" not in accepted or "text/event-stream" not in accepted:
        return _response(_rpc_error(None,-32600,
                         "Accept must include application/json and text/event-stream"),406)
    length=req.headers.get("content-length","")
    if length.isdecimal() and int(length)>_MAX_RPC_BYTES:
        raise HTTPException(413,"MCP request too large")
    body=await req.body()
    if len(body)>_MAX_RPC_BYTES:
        raise HTTPException(413,"MCP request too large")
    try:
        data=json.loads(body)
    except (ValueError,UnicodeError):
        return _response(_rpc_error(None,-32700,"Invalid JSON"),400)
    if not isinstance(data,dict) or data.get("jsonrpc")!="2.0" or not isinstance(data.get("method"),str):
        return _response(_rpc_error(None,-32600,"Invalid JSON-RPC request"),400)
    method=data["method"]
    rpc_id=data.get("id")
    if rpc_id is not None and (type(rpc_id) not in (int,str) or len(str(rpc_id))>100):
        return _response(_rpc_error(None,-32600,"Invalid request ID"),400)
    if "id" not in data:
        if method.startswith("notifications/"):
            return Response(status_code=202,headers={"Cache-Control":"no-store"})
        return _response(_rpc_error(None,-32600,"Expected notification or request ID"),400)
    params=data.get("params") or {}
    if not isinstance(params,dict):
        return _response(_rpc_error(rpc_id,-32602,"Invalid parameters"))
    if method=="initialize":
        offered=params.get("protocolVersion")
        version=offered if offered in _SUPPORTED else "2025-11-25"
        return _response(_rpc_response(rpc_id,{
            "protocolVersion":version,
            "capabilities":{"tools":{"listChanged":False}},
            "serverInfo":{"name":"simpleai-proxy-mcp","version":"1.0.0"},
            "instructions":"Inspect SimpleAI Proxy diagnostics, worker states, Codex failures, "
                           "routing groups and model catalog. Never request API keys or secrets. "
                           "Write tools require operate scope and confirm=true."
        }))
    if method=="ping":
        return _response(_rpc_response(rpc_id,{}))
    if method=="tools/list":
        tools=[]
        for tool in toolset.TOOLS:
            if (principal.get("auth_type") != "oauth" and principal["scope"] != "operate"
                    and not tool["annotations"]["readOnlyHint"]):
                continue  # Preserve legacy read-only API key behavior.
            exposed=dict(tool)
            requested=oauth.READ if tool["annotations"]["readOnlyHint"] else oauth.OPERATE
            exposed["securitySchemes"]=[{"type":"oauth2","scopes":[requested]}]
            exposed["_meta"]={"securitySchemes":[{"type":"oauth2","scopes":[requested]}]}
            tools.append(exposed)
        return _response(_rpc_response(rpc_id,{"tools":tools}))
    if method=="tools/call":
        name=params.get("name")
        args=params.get("arguments",{})
        if name not in toolset.TOOL_BY_NAME or not isinstance(args,dict):
            return _response(_rpc_error(rpc_id,-32602,"Unknown tool or invalid arguments"))
        spec=toolset.TOOL_BY_NAME[name]
        if not spec["annotations"]["readOnlyHint"] and principal["scope"]!="operate":
            if principal.get("auth_type") == "oauth":
                return _response(_rpc_response(rpc_id,{
                    "isError":True,
                    "content":[{"type":"text","text":"Authorize the SimpleAI MCP operate scope to use this tool."}],
                    "_meta":{"mcp/www_authenticate":[oauth.challenge_header(
                        error="insufficient_scope", scope=oauth.OPERATE)]}}))
            return _response(_rpc_error(rpc_id,-32602,"This MCP key is read-only"))
        try:
            result=await toolset.execute(name,args,_app)
            text=json.dumps(result,ensure_ascii=False,default=str,allow_nan=False)
            if len(text)>200000:
                return _response(_rpc_response(rpc_id,{"isError":True,"content":[{
                    "type":"text","text":"Result too large; use a narrower query or limit"}]}))
            return _response(_rpc_response(rpc_id,{
                "content":[{"type":"text","text":text}],
                "structuredContent":result,"isError":False}))
        except Exception as exc:
            safe_logs.record("mcp", "warning", "tool_failed",exception_type=type(exc).__name__)
            from fastapi import HTTPException as FastHTTPException
            category=type(exc).__name__
            status=exc.status_code if isinstance(exc,FastHTTPException) else None
            msg=f"{category}" + (f" HTTP {status}" if status else "")
            # Never echo arbitrary exceptions, URLs, user prompts or credentials.
            return _response(_rpc_response(rpc_id,{
                "isError":True,"content":[{"type":"text","text":"Tool error: "+msg}]}))
    return _response(_rpc_error(rpc_id,-32601,"MCP method not supported"))


@_router.get("/mcp")
async def mcp_get():
    return Response(status_code=405,headers={"Allow":"POST","Cache-Control":"no-store"})


def install(core) -> None:
    global _core,_app,_installed
    if _installed:
        return
    _core=core
    _app=core.app
    _setup()
    oauth.install(core)
    _router.include_router(oauth.router)
    safe_logs.install()
    core.app.include_router(_router)
    _installed=True
