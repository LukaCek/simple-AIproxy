"""Admin UI and API for inspecting Codex account usage limits."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

import live_logs_entrypoint as _live
import main as _impl

app = _live.app


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode JWT claims without verification; used only to read account metadata."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _find_account_id(value: Any) -> str:
    if isinstance(value, dict):
        preferred = (
            "chatgpt_account_id",
            "account_id",
            "chatgptAccountId",
            "accountId",
        )
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for key, candidate in value.items():
            if "account" in str(key).lower() and isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for candidate in value.values():
            found = _find_account_id(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_account_id(candidate)
            if found:
                return found
    return ""


def codex_account_id(provider: dict[str, Any]) -> str:
    explicit = str(provider.get("account_id") or provider.get("chatgpt_account_id") or "").strip()
    if explicit:
        return explicit
    token = str(provider.get("access_token") or provider.get("api_key") or "").strip()
    if not token:
        return ""
    return _find_account_id(_decode_jwt_payload(token))


def _window_label(seconds: Any, fallback: str) -> str:
    try:
        duration = float(seconds)
    except (TypeError, ValueError):
        return fallback
    if duration <= 0:
        return fallback
    known = (
        (5 * 3600, "5 hours"),
        (24 * 3600, "Daily"),
        (7 * 24 * 3600, "Weekly"),
        (30 * 24 * 3600, "Monthly"),
    )
    for expected, label in known:
        if abs(duration - expected) / expected <= 0.10:
            return label
    if duration < 86400:
        hours = duration / 3600
        return f"{hours:g} hours"
    days = duration / 86400
    return f"{days:g} days"


def _normalize_window(raw: Any, kind: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used = raw.get("used_percent", raw.get("usedPercent"))
    seconds = raw.get("limit_window_seconds")
    if seconds is None:
        minutes = raw.get("window_minutes", raw.get("windowDurationMins"))
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
            seconds = float(minutes) * 60
    reset_at = raw.get("reset_at", raw.get("resets_at", raw.get("resetsAt")))
    try:
        used_value = max(0.0, min(100.0, float(used))) if used is not None else None
    except (TypeError, ValueError):
        used_value = None
    try:
        reset_value = float(reset_at) if reset_at is not None else None
    except (TypeError, ValueError):
        reset_value = None
    return {
        "kind": kind,
        "label": _window_label(seconds, kind.title()),
        "used_percent": used_value,
        "remaining_percent": round(100.0 - used_value, 1) if used_value is not None else None,
        "window_seconds": seconds,
        "reset_at": reset_value,
        "reset_iso": datetime.fromtimestamp(reset_value, tz=timezone.utc).isoformat() if reset_value else None,
        "reset_after_seconds": max(0, int(reset_value - time.time())) if reset_value else raw.get("reset_after_seconds"),
    }


def normalize_codex_usage(data: dict[str, Any]) -> dict[str, Any]:
    rate_limit = data.get("rate_limit") or data.get("rateLimit") or {}
    windows: list[dict[str, Any]] = []
    if isinstance(rate_limit, dict):
        candidates = (
            ("primary", rate_limit.get("primary_window") or rate_limit.get("primary")),
            ("secondary", rate_limit.get("secondary_window") or rate_limit.get("secondary")),
        )
        for kind, raw in candidates:
            window = _normalize_window(raw, kind)
            if window:
                windows.append(window)

    additional: list[dict[str, Any]] = []
    for item in data.get("additional_rate_limits", []) if isinstance(data.get("additional_rate_limits"), list) else []:
        if not isinstance(item, dict):
            continue
        item_rl = item.get("rate_limit") or {}
        item_windows = []
        if isinstance(item_rl, dict):
            for kind, raw in (
                ("primary", item_rl.get("primary_window") or item_rl.get("primary")),
                ("secondary", item_rl.get("secondary_window") or item_rl.get("secondary")),
            ):
                window = _normalize_window(raw, kind)
                if window:
                    item_windows.append(window)
        additional.append({"name": item.get("limit_name") or item.get("name") or "Additional limit", "windows": item_windows})

    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    return {
        "plan_type": data.get("plan_type") or data.get("planType") or "unknown",
        "allowed": rate_limit.get("allowed") if isinstance(rate_limit, dict) else None,
        "limit_reached": bool(rate_limit.get("limit_reached")) if isinstance(rate_limit, dict) else False,
        "windows": windows,
        "additional_rate_limits": additional,
        "credits": {
            "has_credits": bool(credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_codex_usage(provider_name: str) -> dict[str, Any]:
    provider = _impl.find_provider(provider_name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
    if provider.get("api_mode") != "codex_responses":
        raise HTTPException(status_code=400, detail=f"Provider '{provider_name}' is not a Codex provider")

    endpoint = _impl.provider_to_endpoint(provider)
    try:
        await _impl.ensure_provider_token(endpoint)
    except Exception:
        # The normal request path will surface a useful error below if refresh fails.
        pass
    provider = _impl.find_provider(provider_name) or provider
    access_token = str(provider.get("access_token") or provider.get("api_key") or "").strip()
    if not access_token:
        raise HTTPException(status_code=401, detail="Codex profile is not authenticated")
    account_id = codex_account_id(provider)
    if not account_id:
        raise HTTPException(status_code=400, detail="Could not determine ChatGPT account ID from this Codex login. Reauthenticate the profile.")
    if _impl.http_client is None:
        raise HTTPException(status_code=500, detail="HTTP client is not initialized")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "ChatGPT-Account-Id": account_id,
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    urls = (
        "https://chatgpt.com/backend-api/codex/usage",
        "https://chatgpt.com/backend-api/wham/usage",
    )
    last_error = ""
    for url in urls:
        try:
            response = await _impl.http_client.get(url, headers=headers, timeout=30.0)
        except Exception as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            continue
        if response.status_code == 200:
            try:
                payload = response.json()
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Codex usage returned invalid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=502, detail="Codex usage returned an unexpected response")
            normalized = normalize_codex_usage(payload)
            normalized["provider"] = provider_name
            return normalized
        if response.status_code == 404:
            last_error = f"{url} returned 404"
            continue
        text = response.text[:1000]
        if response.status_code in {401, 403}:
            _impl.mark_provider_reauth_required(provider_name, f"Codex usage authentication failed ({response.status_code}). Reauthenticate this profile.")
        raise HTTPException(status_code=response.status_code, detail=text or f"Codex usage request failed ({response.status_code})")
    raise HTTPException(status_code=502, detail=last_error or "Codex usage endpoint is unavailable")


@app.get("/admin/codex-usage", response_class=HTMLResponse, dependencies=[Depends(_impl.verify_admin)])
async def admin_codex_usage(request: Request) -> Any:
    providers = [p for p in _impl.get_providers() if p.get("is_codex_oauth")]
    return _impl.templates.TemplateResponse(
        request=request,
        name="codex_usage.html",
        context={"providers": providers},
    )


@app.get("/admin/codex-usage/{provider_name}.json", dependencies=[Depends(_impl.verify_admin)])
async def admin_codex_usage_json(provider_name: str) -> JSONResponse:
    data = await fetch_codex_usage(provider_name)
    return JSONResponse(data)
