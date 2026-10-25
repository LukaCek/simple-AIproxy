"""Account-scoped Codex model discovery, quota persistence and runtime routing.

The Codex /models catalog is account-specific but is not an entitlement guarantee.
A model_not_found inference response is therefore remembered separately.
No OAuth token or response body is persisted in the registry database.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import codex_usage_entrypoint as usage

_impl: Any = None
_model_task: asyncio.Task | None = None
_usage_task: asyncio.Task | None = None
_original_usage_fetch: Any = None
_original_resolver: Any = None
_original_headers: Any = None
_original_provider_list: Any = None
_original_startup: Any = None
_original_shutdown: Any = None

MODEL_REFRESH_SECONDS = max(60, int(os.getenv("CODEX_MODEL_REFRESH_SECONDS", "300")))
USAGE_REFRESH_SECONDS = max(60, int(os.getenv("CODEX_USAGE_REFRESH_SECONDS", "180")))
CLIENT_VERSION = os.getenv("CODEX_CLIENT_VERSION", "0.162.1")
CATALOG_STALE_SECONDS = max(300, int(os.getenv("CODEX_CATALOG_MAX_AGE_SECONDS", "86400")))
REJECTED_MODEL_SECONDS = max(60, int(os.getenv("CODEX_REJECTED_MODEL_SECONDS", "1800")))


def _conn():
    return _impl.get_db_connection()


def init_schema() -> None:
    with _conn() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS CodexCatalogs (
                provider TEXT PRIMARY KEY,
                etag TEXT, checked_at REAL, updated_at REAL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS CodexModels (
                provider TEXT NOT NULL, slug TEXT NOT NULL,
                display_name TEXT NOT NULL, position INTEGER NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, slug)
            );
            CREATE TABLE IF NOT EXISTS CodexQuota (
                provider TEXT PRIMARY KEY,
                payload TEXT NOT NULL, checked_at REAL NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS CodexBlocks (
                provider TEXT NOT NULL, slug TEXT NOT NULL,
                reason TEXT NOT NULL, until_at REAL NOT NULL,
                PRIMARY KEY (provider, slug)
            );
        """)


def parse_models(data: Any) -> list[dict[str, Any]]:
    """Accept only a valid Codex models envelope, never a generic error object."""
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("Codex catalog is missing a models array")
    found = set()
    result = []
    for item in data["models"]:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug or slug in found or len(slug) > 160:
            continue
        if str(item.get("visibility") or "list").lower() in {"hide", "hidden"}:
            continue
        found.add(slug)
        result.append({
            "slug": slug,
            "display_name": str(item.get("display_name") or slug)[:160],
            "is_default": bool(item.get("is_default")),
        })
    return result


def catalog_models(provider: str) -> list[dict[str, Any]]:
    try:
        with _conn() as db:
            info = db.execute(
                "SELECT updated_at FROM CodexCatalogs WHERE provider=?", (provider,)
            ).fetchone()
            if not info or not info["updated_at"] or time.time() - info["updated_at"] > CATALOG_STALE_SECONDS:
                return []
            rows = db.execute(
                "SELECT slug, display_name, is_default FROM CodexModels "
                "WHERE provider=? ORDER BY position", (provider,)
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception:
        return []


def all_codex_models() -> dict[str, list[dict[str, Any]]]:
    with _impl.config_lock:
        names = [
            str(p.get("name")) for p in _impl.config_data.get("providers", [])
            if isinstance(p, dict) and p.get("api_mode") == "codex_responses"
        ]
    return {name: catalog_models(name) for name in names}


def block_until(provider: str, model: str) -> float:
    now = time.time()
    try:
        with _conn() as db:
            row = db.execute(
                "SELECT MAX(until_at) AS until_at FROM CodexBlocks "
                "WHERE provider=? AND slug IN (?, '*') AND until_at>?",
                (provider, model, now),
            ).fetchone()
            return float(row["until_at"] or 0)
    except Exception:
        return 0


def record_block(provider: str, model: str, reason: str, until_at: float) -> None:
    if until_at <= time.time():
        return
    with _conn() as db:
        db.execute(
            "INSERT INTO CodexBlocks(provider,slug,reason,until_at) VALUES (?,?,?,?) "
            "ON CONFLICT(provider,slug) DO UPDATE SET reason=excluded.reason, "
            "until_at=MAX(CodexBlocks.until_at,excluded.until_at)",
            (provider, model, reason, until_at),
        )


def _error_info(text: str) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return "", {}
        err = payload.get("error")
        if not isinstance(err, dict):
            err = {}
        return str(err.get("code") or err.get("type") or ""), err
    except (ValueError, TypeError):
        return "", {}


def note_error(endpoint: dict[str, Any], status_code: int, body: str) -> bool:
    """Return True only when a 400 is safely retryable on another account."""
    if endpoint.get("api_mode") != "codex_responses":
        return False
    name = str(endpoint.get("name") or "")
    model = str(endpoint.get("model") or "")
    code, err = _error_info(body)
    if status_code == 400 and code == "model_not_found":
        record_block(name, model, "model_not_found", time.time() + REJECTED_MODEL_SECONDS)
        return True
    if status_code == 429:
        reset_at = err.get("resets_at") or err.get("reset_at")
        try:
            until = float(reset_at)
        except (ValueError, TypeError):
            until = time.time() + 60
        if until <= time.time():
            until = time.time() + 60
        # Account's shared Codex window. Specific additional pools can still be
        # learned from the usage endpoint without claiming per-model guarantees.
        key = "*" if code == "usage_limit_reached" else model
        record_block(name, key, code or "rate_limit", until)
    return False


def note_success(endpoint: dict[str, Any]) -> None:
    if endpoint.get("api_mode") != "codex_responses":
        return
    with _conn() as db:
        db.execute(
            "DELETE FROM CodexBlocks WHERE provider=? AND slug=?",
            (endpoint.get("name"), endpoint.get("model")),
        )


def _eligible(provider: dict[str, Any], model: str, discovered: dict[str, Any]) -> bool:
    if provider.get("api_mode") != "codex_responses":
        return True
    # Fail closed if discovery has never succeeded or the catalog has expired.
    if model not in discovered.get(str(provider.get("name")), set()):
        return False
    if provider.get("oauth_reauth_required"):
        return False
    return block_until(str(provider.get("name")), model) <= time.time()


def resolve_requested_model(name: str) -> list[dict[str, Any]]:
    with _impl.config_lock:
        providers = {
            str(p.get("name")): dict(p)
            for p in _impl.config_data.get("providers", [])
            if isinstance(p, dict) and p.get("name")
        }
        groups = dict(_impl.config_data.get("groups") or {})
    cached = all_codex_models()
    discovered = {key: {m["slug"] for m in models} for key, models in cached.items()}

    def endpoint(provider_name: str, model: str) -> list[dict[str, Any]]:
        p = providers.get(provider_name)
        if p is None or not _eligible(p, model, discovered):
            return []
        return [_impl.provider_to_endpoint(p, model)]

    def pool(model: str) -> list[dict[str, Any]]:
        return [
            item for p in providers.values()
            for item in endpoint(str(p["name"]), model)
            if p.get("api_mode") == "codex_responses"
        ]

    def codex_default() -> list[dict[str, Any]]:
        # Each account may have a different preferred default.
        out = []
        for provider, models in cached.items():
            if models:
                model = next((m["slug"] for m in models if m["is_default"]), models[0]["slug"])
                out.extend(endpoint(provider, model))
        return out

    def group(key: str, seen: set[str]) -> list[dict[str, Any]]:
        if key in seen:
            raise HTTPException(status_code=400, detail=f"Group cycle at {key}")
        if key == "codex":
            return codex_default()
        setting = groups.get(key)
        if not isinstance(setting, dict):
            # Dynamic discovered models can be used even before they appear
            # in config.yml, while legacy Codex lists cannot grant access.
            return pool(key)
        out = []
        for member in setting.get("members", []):
            if not isinstance(member, dict):
                continue
            if member.get("group"):
                out.extend(group(str(member["group"]), seen | {key}))
            elif member.get("provider"):
                out.extend(endpoint(str(member["provider"]), str(member.get("model") or "")))
        return out

    if name in groups or name == "codex":
        result = group(name, set())
    else:
        result = pool(name)
        for p in providers.values():
            if p.get("api_mode") != "codex_responses" and name in [str(m) for m in p.get("models", [])]:
                result.extend(endpoint(str(p["name"]), name))
    if not result:
        raise HTTPException(status_code=503, detail=f"No eligible provider for model '{name}' (catalog/usage/access)")
    # De-duplicate nested group aliases while preserving round robin.
    unique = {(e["name"], e["model"]): e for e in result}
    return _impl.route_endpoints(name, list(unique.values()))


def provider_headers(endpoint: dict[str, Any]) -> dict[str, str]:
    headers = _original_headers(endpoint)
    if endpoint.get("api_mode") == "codex_responses":
        live = _impl.find_provider(str(endpoint.get("name") or ""))
        account_id = usage.codex_account_id(live or endpoint)
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        headers["originator"] = "codex_cli_rs"
    return headers


async def refresh_catalog(provider_name: str) -> None:
    p = _impl.find_provider(provider_name)
    if not p or p.get("api_mode") != "codex_responses" or _impl.http_client is None:
        return
    checked = time.time()
    etag = ""
    with _conn() as db:
        old = db.execute("SELECT etag FROM CodexCatalogs WHERE provider=?", (provider_name,)).fetchone()
        if old:
            etag = str(old["etag"] or "")
    try:
        endpoint = _impl.provider_to_endpoint(p)
        await _impl.ensure_provider_token(endpoint)
        headers = provider_headers(endpoint)
        headers["Accept"] = "application/json"
        if etag:
            headers["If-None-Match"] = etag
        base = str(p.get("url") or "").rstrip("/")
        parsed = urlparse(base)
        url = urlunparse(parsed._replace(path=parsed.path.rstrip("/") + "/models", query=""))
        response = await _impl.http_client.get(
            url, headers=headers, params={"client_version": CLIENT_VERSION}, timeout=20.0
        )
        if response.status_code == 304:
            with _conn() as db:
                db.execute(
                    "UPDATE CodexCatalogs SET checked_at=?, updated_at=?, error=NULL WHERE provider=?",
                    (checked, checked, provider_name),
                )
            return
        response.raise_for_status()
        models = parse_models(response.json())
        next_etag = response.headers.get("ETag") or ""
        with _conn() as db:
            db.execute(
                "INSERT INTO CodexCatalogs(provider,etag,checked_at,updated_at,error) "
                "VALUES (?,?,?,?,NULL) ON CONFLICT(provider) DO UPDATE SET "
                "etag=excluded.etag,checked_at=excluded.checked_at,"
                "updated_at=excluded.updated_at,error=NULL",
                (provider_name, next_etag, checked, checked),
            )
            db.execute("DELETE FROM CodexModels WHERE provider=?", (provider_name,))
            db.executemany(
                "INSERT INTO CodexModels(provider,slug,display_name,position,is_default) "
                "VALUES (?,?,?,?,?)",
                [(provider_name, m["slug"], m["display_name"], i, int(m["is_default"])) for i, m in enumerate(models)],
            )
            # An actual catalog change may make previously rejected models available.
            if etag != next_etag:
                db.execute(
                    "DELETE FROM CodexBlocks WHERE provider=? AND reason='model_not_found'",
                    (provider_name,),
                )
    except Exception as exc:
        # Never delete a previously successful catalog on transient failures.
        with _conn() as db:
            db.execute(
                "INSERT INTO CodexCatalogs(provider,checked_at,error) VALUES (?,?,?) "
                "ON CONFLICT(provider) DO UPDATE SET checked_at=excluded.checked_at,error=excluded.error",
                (provider_name, checked, f"{type(exc).__name__}: {str(exc)[:160]}"),
            )


def usage_payload(provider: str) -> dict[str, Any]:
    with _conn() as db:
        row = db.execute(
            "SELECT payload,checked_at,error FROM CodexQuota WHERE provider=?", (provider,)
        ).fetchone()
    if row:
        data = json.loads(row["payload"])
        data["usage_checked_at"] = row["checked_at"]
        data["usage_error"] = row["error"]
        return data
    return {"provider": provider, "plan_type": "unknown", "windows": [], "usage_error": "Not yet checked"}


async def refresh_usage(provider_name: str) -> None:
    try:
        data = await _original_usage_fetch(provider_name)
        if data.get("limit_reached"):
            windows = [w for w in data.get("windows", []) if isinstance(w, dict)]
            breached = [w for w in windows if (w.get("remaining_percent") is not None and w["remaining_percent"] <= 0)]
            resets = [float(w.get("reset_at") or 0) for w in (breached or windows)]
            future = [ts for ts in resets if ts > time.time()]
            if future:
                record_block(provider_name, "*", "usage_limit_reached", max(future))
        with _conn() as db:
            db.execute(
                "INSERT INTO CodexQuota(provider,payload,checked_at,error) VALUES (?,?,?,NULL) "
                "ON CONFLICT(provider) DO UPDATE SET payload=excluded.payload,"
                "checked_at=excluded.checked_at,error=NULL",
                (provider_name, json.dumps(data), time.time()),
            )
    except Exception as exc:
        with _conn() as db:
            db.execute(
                "UPDATE CodexQuota SET error=? WHERE provider=?",
                (f"{type(exc).__name__}: {str(exc)[:160]}", provider_name),
            )


def account_view(provider_name: str) -> dict[str, Any]:
    result = usage_payload(provider_name)
    with _conn() as db:
        row = db.execute(
            "SELECT checked_at, updated_at, error FROM CodexCatalogs WHERE provider=?", (provider_name,)
        ).fetchone()
    result["catalog"] = {
        "checked_at": row["checked_at"] if row else None,
        "updated_at": row["updated_at"] if row else None,
        "error": row["error"] if row else "Discovery pending",
    }
    with _conn() as db:
        active_blocks = {
            r["slug"]: dict(r)
            for r in db.execute(
                "SELECT slug,reason,until_at FROM CodexBlocks "
                "WHERE provider=? AND until_at>?", (provider_name, time.time())
            ).fetchall()
        }
    result["models"] = []
    for m in catalog_models(provider_name):
        block = active_blocks.get(m["slug"]) or active_blocks.get("*")
        result["models"].append({
            **m,
            "status": ("unsupported" if block and block["reason"] == "model_not_found"
                       else "limited" if block else "available"),
            "available_after": block["until_at"] if block else None,
            "reason": block["reason"] if block else None,
        })
    return result


async def cached_usage(provider_name: str) -> dict[str, Any]:
    return account_view(provider_name)


async def _model_loop() -> None:
    while True:
        names = list(all_codex_models())
        await asyncio.gather(*(refresh_catalog(n) for n in names), return_exceptions=True)
        await asyncio.sleep(MODEL_REFRESH_SECONDS)


async def _usage_loop() -> None:
    while True:
        names = list(all_codex_models())
        await asyncio.gather(*(refresh_usage(n) for n in names), return_exceptions=True)
        await asyncio.sleep(USAGE_REFRESH_SECONDS)


async def startup() -> None:
    global _model_task, _usage_task
    await _original_startup()
    init_schema()
    _model_task = asyncio.create_task(_model_loop())
    _usage_task = asyncio.create_task(_usage_loop())


async def shutdown() -> None:
    for task in (_model_task, _usage_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await _original_shutdown()


def providers_list() -> list[dict[str, Any]]:
    result = _original_provider_list()
    for p in result:
        if p.get("is_codex_oauth"):
            p["models"] = [m["slug"] for m in catalog_models(str(p.get("name")))]
    return result


async def listed_models(api_key_record: Any = Depends(lambda: None)) -> dict[str, Any]:
    # A real auth dependency is installed by the route replacement below.
    del api_key_record
    models = set()
    with _impl.config_lock:
        for p in _impl.config_data.get("providers", []):
            if isinstance(p, dict) and p.get("api_mode") != "codex_responses":
                models.update(str(m) for m in p.get("models", []))
        models.update(str(x) for x in _impl.config_data.get("model_profiles", {}))
        groups = list(_impl.config_data.get("groups", {}))
    for models_per_account in all_codex_models().values():
        models.update(m["slug"] for m in models_per_account)
    for group in groups:
        try:
            if resolve_requested_model(group):
                models.add(group)
        except HTTPException:
            pass
    return {"object": "list", "data": [
        {"id": m, "object": "model", "created": 0, "owned_by": "simple-aiproxy"}
        for m in sorted(models)
    ]}


def install(impl: Any) -> None:
    global _impl, _original_usage_fetch, _original_resolver, _original_headers
    global _original_provider_list, _original_startup, _original_shutdown
    _impl = impl
    _original_usage_fetch = usage.fetch_codex_usage
    _original_resolver = impl.resolve_requested_model
    _original_headers = impl.build_provider_headers
    _original_provider_list = impl.get_providers
    _original_startup = impl.startup
    _original_shutdown = impl.shutdown
    impl.resolve_requested_model = resolve_requested_model
    impl.build_provider_headers = provider_headers
    impl.get_providers = providers_list
    original_models_for_provider = impl.get_models_for_provider

    def models_for_provider(name: str) -> list[str]:
        provider = impl.find_provider(name)
        if provider and provider.get("api_mode") == "codex_responses":
            return [m["slug"] for m in catalog_models(name)]
        return original_models_for_provider(name)

    impl.get_models_for_provider = models_for_provider

    original_upsert = impl.upsert_codex_profile

    def update_codex_profile(name: str, *args: Any, **kwargs: Any) -> None:
        original_upsert(name, *args, **kwargs)
        # A re-login can change the ChatGPT identity under the same name.
        # Discard old identity-specific catalogs and quota windows.
        with _conn() as db:
            for table in ("CodexModels", "CodexCatalogs", "CodexQuota", "CodexBlocks"):
                db.execute(f"DELETE FROM {table} WHERE provider=?", (name,))
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(refresh_catalog(name))
            loop.create_task(refresh_usage(name))
        except RuntimeError:
            pass

    impl.upsert_codex_profile = update_codex_profile
    impl.codex_note_error = note_error
    impl.codex_note_success = note_success
    usage.fetch_codex_usage = cached_usage
    # FastAPI records startup/shutdown callables when declared. Reassigning
    # module symbols does not replace those hooks; append separate hooks instead.
    @impl.app.on_event("startup")
    async def codex_registry_startup() -> None:
        global _model_task, _usage_task
        init_schema()
        _model_task = asyncio.create_task(_model_loop())
        _usage_task = asyncio.create_task(_usage_loop())

    @impl.app.on_event("shutdown")
    async def codex_registry_shutdown() -> None:
        for task in (_model_task, _usage_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass



    # Remove only the outdated static model-list route; preserve its authentication.
    from fastapi import Depends
    app = impl.app
    app.router.routes[:] = [
        r for r in app.router.routes
        if not (getattr(r, "path", None) == "/v1/models" and "GET" in (getattr(r, "methods", None) or set()))
    ]

    async def model_list(api_key_record: Any = Depends(impl.validate_api_key)):
        return await listed_models(api_key_record)
    app.add_api_route("/v1/models", model_list, methods=["GET"])

    @app.post("/admin/codex-usage/refresh", dependencies=[Depends(impl.verify_admin)])
    async def refresh_now() -> JSONResponse:
        names = list(all_codex_models())
        await asyncio.gather(*(refresh_catalog(n) for n in names), return_exceptions=True)
        await asyncio.gather(*(refresh_usage(n) for n in names), return_exceptions=True)
        return JSONResponse({"ok": True, "profiles": len(names)})
