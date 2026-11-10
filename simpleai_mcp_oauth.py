"""OAuth 2.1 authorization-code + PKCE for the SimpleAI remote MCP server.

Designed for a single-owner SimpleAI Proxy with existing Basic-auth admin.
Uses public OAuth clients via RFC 7591 Dynamic Client Registration (ChatGPT
compatible). Secrets/codes/tokens are SHA256 hashed in persistent SQLite.
Explicit, CSRF-protected consent; one-time codes; rotating refresh tokens;
no client-credentials grant, no implicit flow and no token forwarding.

The issuer and resource are FIXED HTTPS URLs, never derived from Host or
untrusted X-Forwarded-* headers. OAuth does not bypass MCP scope checks.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

router = APIRouter(include_in_schema=False)
_core = None
READ = "simpleai:read"
OPERATE = "simpleai:operate"
OFFLINE = "offline_access"
SCOPES = frozenset((READ, OPERATE, OFFLINE))
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
PENDING_TTL = 600
CLIENT_LIMIT = 200
AUTH_MAX_BYTES = 4096
DEFAULT_ISSUER = "https://aiproxy.cekluka.com"


def issuer() -> str:
    value = os.getenv("SIMPLEAI_MCP_OAUTH_ISSUER", DEFAULT_ISSUER).rstrip("/")
    split = urlsplit(value)
    if (split.scheme != "https" or not split.hostname or split.username or
            split.password or split.query or split.fragment or split.path or
            split.port not in (None, 443)):
        raise RuntimeError("MCP OAuth issuer must be an HTTPS origin without path")
    return value


def resource() -> str:
    return issuer() + "/mcp"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _db():
    if _core is None:
        raise RuntimeError("MCP OAuth is not installed")
    return _core.get_db_connection()


def setup() -> None:
    with _db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS MCPOAuthClients (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            redirect_uris TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS MCPOAuthPending (
            id TEXT PRIMARY KEY,
            csrf_hash TEXT NOT NULL,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            state TEXT NOT NULL,
            scope TEXT NOT NULL,
            resource TEXT NOT NULL,
            challenge TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS MCPOAuthCodes (
            code_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            redirect_uri TEXT NOT NULL,
            scope TEXT NOT NULL,
            resource TEXT NOT NULL,
            challenge TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS MCPOAuthAccess (
            token_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            audience TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            family TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS MCPOAuthRefresh (
            token_hash TEXT PRIMARY KEY,
            client_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            audience TEXT NOT NULL,
            family TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_oauth_access_family
            ON MCPOAuthAccess(family);
        CREATE INDEX IF NOT EXISTS idx_mcp_oauth_refresh_family
            ON MCPOAuthRefresh(family);
        """)


def challenge_header(error: str = "invalid_token", scope: str = READ) -> str:
    # All values fixed constants; no reflected client-provided strings.
    return ('Bearer resource_metadata="' + issuer() +
            '/.well-known/oauth-protected-resource", error="' + error +
            '", error_description="Connect your SimpleAI Proxy account to continue", '
            'scope="' + scope + '"')


def _json(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={
        "Cache-Control": "no-store", "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff"})


def _oauth_error(code: str, status: int = 400) -> JSONResponse:
    return _json({"error": code}, status)


def _valid_redirect(uri: object) -> bool:
    if not isinstance(uri, str) or not 10 <= len(uri) <= 512:
        return False
    try:
        u = urlsplit(uri)
        if (not u.netloc or not u.hostname or u.username or u.password or
                u.fragment or "\\" in uri or any(ord(ch) < 33 for ch in uri)):
            return False
        # RFC 8252 loopback exception for native MCP clients.
        if u.scheme == "http":
            return u.hostname in ("127.0.0.1", "::1", "localhost")
        return u.scheme == "https"
    except ValueError:
        return False


def _parse_scopes(raw: object) -> set[str]:
    if not isinstance(raw, str) or len(raw) > 256:
        raise ValueError("Invalid scope")
    values = set(raw.split()) if raw.strip() else {READ}
    if not values or not values <= SCOPES:
        raise ValueError("Unsupported OAuth scope")
    if OPERATE in values:
        values.add(READ)
    return values


def _validate_client(client_id: str, redirect_uri: str):
    if not isinstance(client_id, str) or len(client_id) > 200:
        raise ValueError("Invalid client_id")
    with _db() as db:
        client = db.execute(
            "SELECT id,name,redirect_uris FROM MCPOAuthClients WHERE id=?",
            (client_id,)).fetchone()
    if not client or redirect_uri not in json.loads(client["redirect_uris"]):
        raise ValueError("Unregistered OAuth client or redirect URI")
    return client


async def _body_form(req: Request, limit: int = AUTH_MAX_BYTES) -> dict:
    if req.headers.get("content-type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ValueError("Expected application/x-www-form-urlencoded")
    body = await req.body()
    if len(body) > limit:
        raise ValueError("Request too large")
    fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=24)
    if any(len(values) != 1 for values in fields.values()):
        raise ValueError("Duplicate form parameter")
    return {k: v[0] for k, v in fields.items()}


def _same_origin_post(req: Request) -> bool:
    origin = req.headers.get("origin", "").rstrip("/")
    if origin:
        return origin == issuer()
    referer = req.headers.get("referer", "")
    return referer.startswith(issuer() + "/oauth/authorize")


def _admin_verified(req: Request) -> None:
    import simpleai_mcp
    simpleai_mcp._admin_dependency(req)


def _gc(db, now: int) -> None:
    db.execute("DELETE FROM MCPOAuthPending WHERE expires_at<?", (now,))
    db.execute("DELETE FROM MCPOAuthCodes WHERE expires_at<?", (now,))
    db.execute("DELETE FROM MCPOAuthAccess WHERE expires_at<?", (now - 86400,))
    db.execute("DELETE FROM MCPOAuthRefresh WHERE expires_at<?", (now - 86400,))


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata():
    return _json({
        "resource": resource(),
        "authorization_servers": [issuer()],
        "scopes_supported": [READ, OPERATE],
        "bearer_methods_supported": ["header"],
        "resource_name": "SimpleAI Proxy MCP",
    })


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    base = issuer()
    return _json({
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "revocation_endpoint": base + "/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [READ, OPERATE, OFFLINE],
    })


@router.post("/oauth/register")
async def register_client(req: Request):
    if req.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return _oauth_error("invalid_client_metadata", 415)
    raw = await req.body()
    if len(raw) > 4096:
        return _oauth_error("invalid_client_metadata")
    try:
        data = json.loads(raw)
    except ValueError:
        return _oauth_error("invalid_client_metadata")
    if not isinstance(data, dict):
        return _oauth_error("invalid_client_metadata")
    name = data.get("client_name", "MCP client")
    redirects = data.get("redirect_uris")
    auth_method = data.get("token_endpoint_auth_method", "none")
    grants = data.get("grant_types", ["authorization_code", "refresh_token"])
    responses = data.get("response_types", ["code"])
    if (not isinstance(name, str) or not 1 <= len(name) <= 90 or
            not isinstance(redirects, list) or not 1 <= len(redirects) <= 8 or
            len(redirects) != len(set(str(x) for x in redirects)) or
            any(not _valid_redirect(u) for u in redirects) or
            auth_method != "none" or
            not isinstance(grants, list) or not set(grants) <= {"authorization_code","refresh_token"} or
            "authorization_code" not in grants or
            responses != ["code"]):
        return _oauth_error("invalid_client_metadata")
    setup()
    now = int(time.time())
    client_id = "simpleai_client_" + secrets.token_urlsafe(28)
    with _db() as db:
        _gc(db, now)
        count = db.execute("SELECT COUNT(*) FROM MCPOAuthClients").fetchone()[0]
        if count >= CLIENT_LIMIT:
            return _oauth_error("registration_not_supported", 429)
        db.execute("INSERT INTO MCPOAuthClients(id,name,redirect_uris,created_at) VALUES(?,?,?,?)",
                   (client_id, name, json.dumps(redirects), now))
    return _json({
        "client_id": client_id, "client_id_issued_at": now,
        "client_name": name, "redirect_uris": redirects,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }, 201)


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize(req: Request):
    _admin_verified(req)
    values = dict(req.query_params)
    client_id = values.get("client_id", "")
    redirect_uri = values.get("redirect_uri", "")
    try:
        client = _validate_client(client_id, redirect_uri)
        scopes = _parse_scopes(values.get("scope", READ))
        state = values.get("state", "")
        challenge = values.get("code_challenge", "")
        if (values.get("response_type") != "code" or
                values.get("code_challenge_method") != "S256" or
                not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge) or
                not 8 <= len(state) <= 256 or
                values.get("resource") != resource()):
            raise ValueError("Invalid OAuth authorization request")
    except ValueError as exc:
        # Never redirect to an unverified callback.
        return HTMLResponse("Invalid OAuth authorization request", status_code=400)
    now = int(time.time())
    nonce = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    with _db() as db:
        _gc(db, now)
        db.execute("""INSERT INTO MCPOAuthPending
            (id,csrf_hash,client_id,redirect_uri,state,scope,resource,challenge,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (nonce, _hash(csrf), client_id, redirect_uri, state,
             " ".join(sorted(scopes)), resource(), challenge, now + PENDING_TTL))
    def esc(v): return html.escape(str(v), quote=True)
    read_only = READ in scopes
    wants_operate = OPERATE in scopes
    wants_offline = OFFLINE in scopes
    choices = (('<option value="read" selected>Read-only diagnostics</option>' +
                '<option value="operate">Read and manage models/workers/groups</option>')
               if wants_operate else '<option value="read">Read-only diagnostics</option>')
    page = f"""<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="referrer" content="no-referrer">
    <title>Authorize SimpleAI MCP</title>
    <style>body{{font:16px system-ui;margin:3rem auto;max-width:620px;
      padding:0 1.5rem;background:#f6f8fb;color:#172234}}
      section{{background:white;padding:2rem;border:1px solid #dae1ec;border-radius:16px}}
      label,select,button{{display:block;margin-top:1rem}}
      select{{padding:.6rem;max-width:100%}}
      button{{padding:.7rem 1.4rem;border:0;border-radius:8px;cursor:pointer}}
      button.approve{{background:#173956;color:white}}
      small{{color:#516070;overflow-wrap:anywhere}}</style></head><body>
    <section><h1>Connect an MCP client to SimpleAI Proxy</h1>
      <p><strong>{esc(client['name'])}</strong> is requesting access to the
      private SimpleAI Proxy administration interface.</p>
      <p>After approval, this client can query your worker diagnostics and
      routing state. Operate access can change models and use provider credits.</p>
      <small>Callback: {esc(redirect_uri)}</small>
      <form action="{esc(issuer())}/oauth/authorize" method="post">
        <input type="hidden" name="authorization_request_id" value="{esc(nonce)}">
        <input type="hidden" name="csrf" value="{esc(csrf)}">
        <label>Permissions<select name="permission">{choices}</select></label>
        <p><small>{'Offline access requested: refreshing the connection for up to 30 days.' if wants_offline else 'No offline access requested.'}</small></p>
        <button class="approve" type="submit" name="decision" value="approve">Authorize</button>
        <button type="submit" name="decision" value="deny">Deny</button>
      </form></section></body></html>"""
    return HTMLResponse(page, headers={
        "Cache-Control": "no-store", "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})


@router.post("/oauth/authorize")
async def authorize_decision(req: Request):
    _admin_verified(req)
    if not _same_origin_post(req):
        raise HTTPException(403, "Invalid consent origin")
    try:
        data = await _body_form(req)
    except (ValueError, UnicodeError):
        return HTMLResponse("Invalid OAuth consent request", status_code=400)
    attempt_id = data.get("authorization_request_id", "")
    csrf = data.get("csrf", "")
    decision = data.get("decision", "")
    permission = data.get("permission", "")
    if (not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", attempt_id) or
            not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", csrf) or
            decision not in ("approve","deny") or
            permission not in ("read","operate")):
        return HTMLResponse("Invalid OAuth consent", status_code=400)
    now = int(time.time())
    with _db() as db:
        # The UPDATE is atomic even under two concurrent browser submissions.
        row = db.execute("SELECT * FROM MCPOAuthPending WHERE id=?", (attempt_id,)).fetchone()
        if (not row or row["consumed"] or row["expires_at"] <= now or
                not secrets.compare_digest(row["csrf_hash"], _hash(csrf))):
            return HTMLResponse("Authorization expired or already used", status_code=400)
        original = set(row["scope"].split())
        if permission == "operate" and OPERATE not in original:
            return HTMLResponse("Operation scope not requested", status_code=400)
        updated = db.execute("UPDATE MCPOAuthPending SET consumed=1 WHERE id=? AND consumed=0",
                             (attempt_id,))
        if updated.rowcount != 1:
            return HTMLResponse("Authorization already used", status_code=400)
        if decision == "deny":
            target = row["redirect_uri"]
            query = {"error": "access_denied", "state": row["state"]}
        else:
            code = "simpleai_code_" + secrets.token_urlsafe(32)
            granted = {READ} if permission == "read" else {READ,OPERATE}
            if OFFLINE in original:
                granted.add(OFFLINE)
            db.execute("""INSERT INTO MCPOAuthCodes
                (code_hash,client_id,redirect_uri,scope,resource,challenge,expires_at)
                VALUES(?,?,?,?,?,?,?)""",
                (_hash(code),row["client_id"],row["redirect_uri"],
                 " ".join(sorted(granted)),row["resource"],row["challenge"],
                 now + CODE_TTL))
            target = row["redirect_uri"]
            query = {"code": code, "state": row["state"]}
    sep = "&" if "?" in target else "?"
    return RedirectResponse(target + sep + urlencode(query), status_code=303,
                            headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer"})


def _new_tokens(db, client_id: str, scope: str, family: str, now: int) -> dict:
    access = "simpleai_oauth_" + secrets.token_urlsafe(36)
    db.execute("""INSERT INTO MCPOAuthAccess
        (token_hash,client_id,scope,audience,created_at,expires_at,family)
        VALUES(?,?,?,?,?,?,?)""",
        (_hash(access), client_id, scope, resource(), now, now+ACCESS_TTL, family))
    result = {"access_token": access,"token_type":"Bearer","expires_in":ACCESS_TTL,
              "scope":scope}
    if OFFLINE in scope.split():
        refresh = "simpleai_refresh_" + secrets.token_urlsafe(36)
        db.execute("""INSERT INTO MCPOAuthRefresh
            (token_hash,client_id,scope,audience,family,expires_at)
            VALUES(?,?,?,?,?,?)""",
            (_hash(refresh),client_id,scope,resource(),family,now+REFRESH_TTL))
        result["refresh_token"] = refresh
    return result


@router.post("/oauth/token")
async def token_exchange(req: Request):
    try:
        data = await _body_form(req)
    except (ValueError, UnicodeError):
        return _oauth_error("invalid_request")
    client_id = data.get("client_id")
    requested_resource = data.get("resource")
    grant_type = data.get("grant_type")
    if not isinstance(client_id,str) or requested_resource != resource():
        return _oauth_error("invalid_request")
    if any(k in data for k in ("client_secret","client_assertion")):
        return _oauth_error("invalid_client",401)
    now = int(time.time())
    setup()
    if grant_type == "authorization_code":
        code = data.get("code","")
        verifier = data.get("code_verifier","")
        redirect = data.get("redirect_uri","")
        if (not code.startswith("simpleai_code_") or
                not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or
                not _valid_redirect(redirect)):
            return _oauth_error("invalid_grant")
        actual = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        with _db() as db:
            row = db.execute("SELECT * FROM MCPOAuthCodes WHERE code_hash=?",(_hash(code),)).fetchone()
            if (not row or row["consumed"] or row["expires_at"] <= now or
                    row["client_id"]!=client_id or row["redirect_uri"]!=redirect or
                    row["resource"]!=resource() or
                    not secrets.compare_digest(row["challenge"],actual)):
                return _oauth_error("invalid_grant")
            updated = db.execute("UPDATE MCPOAuthCodes SET consumed=1 "
                                 "WHERE code_hash=? AND consumed=0",(_hash(code),))
            if updated.rowcount != 1:
                return _oauth_error("invalid_grant")
            return _json(_new_tokens(db,client_id,row["scope"],uuid.uuid4().hex,now))
    if grant_type == "refresh_token":
        refresh = data.get("refresh_token","")
        if not isinstance(refresh,str) or not refresh.startswith("simpleai_refresh_"):
            return _oauth_error("invalid_grant")
        with _db() as db:
            row = db.execute("SELECT * FROM MCPOAuthRefresh WHERE token_hash=?",
                             (_hash(refresh),)).fetchone()
            if (not row or row["revoked"] or row["expires_at"] <= now or
                    row["client_id"]!=client_id or row["audience"]!=resource()):
                return _oauth_error("invalid_grant")
            updated = db.execute("UPDATE MCPOAuthRefresh SET revoked=1 "
                                 "WHERE token_hash=? AND revoked=0",(_hash(refresh),))
            if updated.rowcount != 1:
                return _oauth_error("invalid_grant")
            return _json(_new_tokens(db,client_id,row["scope"],row["family"],now))
    return _oauth_error("unsupported_grant_type")


@router.post("/oauth/revoke")
async def revoke(req: Request):
    try:
        data = await _body_form(req)
    except (ValueError, UnicodeError):
        return _oauth_error("invalid_request")
    client_id = data.get("client_id","")
    value = data.get("token","")
    if not isinstance(value,str) or len(value)>256 or not isinstance(client_id,str):
        return _oauth_error("invalid_request")
    h = _hash(value)
    with _db() as db:
        # A public client may revoke only a token it owns. Avoid leaking
        # whether another client's token exists.
        db.execute("UPDATE MCPOAuthAccess SET revoked=1 WHERE token_hash=? AND client_id=?",
                   (h,client_id))
        db.execute("UPDATE MCPOAuthRefresh SET revoked=1 WHERE token_hash=? AND client_id=?",
                   (h,client_id))
    return Response(status_code=200,headers={"Cache-Control":"no-store"})


def validate_access(token: str) -> dict | None:
    if not isinstance(token,str) or not re.fullmatch(r"simpleai_oauth_[A-Za-z0-9_-]{30,128}",token):
        return None
    setup()
    with _db() as db:
        row = db.execute("SELECT token_hash,client_id,scope,audience,expires_at,revoked "
                         "FROM MCPOAuthAccess WHERE token_hash=?",(_hash(token),)).fetchone()
        if not row or row["revoked"] or row["expires_at"]<=int(time.time()) or row["audience"]!=resource():
            return None
        granted=set(row["scope"].split())
        if READ not in granted:
            return None
        return {"id":row["token_hash"],"scope":"operate" if OPERATE in granted else "read",
                "client_id":row["client_id"],"expires_at":row["expires_at"],
                "auth_type":"oauth"}


def install(core) -> None:
    global _core
    _core=core
    setup()
