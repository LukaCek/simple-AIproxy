"""OpenAI-compatible audio transcription proxy routes.

This extension keeps speech-to-text provider credentials on the AIProxy server
and exposes the standard ``POST /v1/audio/transcriptions`` endpoint to clients.
Providers opt in with ``api_mode: openai_audio_transcriptions``.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import Response

import main_impl as _impl

MAX_AUDIO_BYTES = int(os.getenv("AIPROXY_MAX_AUDIO_BYTES", str(25 * 1024 * 1024)))
AUDIO_TIMEOUT_SECONDS = float(os.getenv("AIPROXY_AUDIO_TIMEOUT_SECONDS", "120"))
FALLBACK_STATUSES = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}


def resolve_audio_transcription_url(endpoint: dict[str, Any]) -> str:
    raw_url = str(endpoint.get("url") or "").strip().rstrip("/")
    if not raw_url:
        raise HTTPException(status_code=400, detail="Transcription provider URL is missing")
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Transcription provider URL is invalid")
    path = (parsed.path or "").rstrip("/")
    if not path.endswith("/audio/transcriptions"):
        if path.endswith("/v1"):
            path += "/audio/transcriptions"
        else:
            path += "/v1/audio/transcriptions"
    return urlunparse(parsed._replace(path=path))


def _live_provider(name: str) -> dict[str, Any] | None:
    for provider in _impl.config_data.get("providers", []):
        if isinstance(provider, dict) and str(provider.get("name") or "") == name:
            return provider
    return None


def resolve_audio_endpoints(model: str) -> list[dict[str, Any]]:
    """Resolve a client-facing model/group to transcription-capable providers."""
    try:
        resolved = _impl.resolve_requested_model(model)
    except HTTPException:
        resolved = []

    endpoints: list[dict[str, Any]] = []
    for endpoint in resolved:
        provider = _live_provider(str(endpoint.get("name") or ""))
        merged = dict(provider or {})
        merged.update(endpoint)
        if str(merged.get("api_mode") or "") == "openai_audio_transcriptions":
            endpoints.append(merged)

    # Direct fallback also preserves provider-only fields such as api_key_env.
    if not endpoints:
        for provider in _impl.config_data.get("providers", []):
            if not isinstance(provider, dict):
                continue
            if str(provider.get("api_mode") or "") != "openai_audio_transcriptions":
                continue
            models = [str(value) for value in provider.get("models", []) if value is not None]
            if model in models:
                endpoint = dict(provider)
                endpoint["model"] = model
                endpoints.append(endpoint)

    if not endpoints:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transcription model '{model}' is not configured",
        )
    return endpoints


def transcription_headers(endpoint: dict[str, Any]) -> dict[str, str]:
    env_name = str(endpoint.get("api_key_env") or "").strip()
    api_key = os.getenv(env_name, "").strip() if env_name else ""
    if not api_key:
        api_key = str(endpoint.get("api_key") or "").strip()
    headers = {"Accept": "application/json", "User-Agent": "aiproxy/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _safe_insert_log(
    api_key_record: Any,
    requested_model: str,
    provider_name: str,
    provider_model: str,
    status_code_value: int,
    started_at: str,
    started_monotonic: float,
    filename: str,
    output: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    try:
        ended_at = datetime.utcnow().isoformat()
        total_ms = (time.monotonic() - started_monotonic) * 1000
        api_key_value = api_key_record["key"]
        api_key_name = api_key_record["name"] if "name" in api_key_record.keys() else None
        _impl.insert_log(
            api_key_value,
            api_key_name,
            requested_model,
            requested_model,
            provider_name,
            provider_model,
            status_code_value,
            started_at,
            ended_at,
            ended_at,
            total_ms,
            total_ms,
            f"[audio transcription] {filename}",
            output,
            error,
        )
    except Exception:
        # Logging must never break transcription.
        pass


@_impl.app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    file: UploadFile = File(...),
    model: str = Form(...),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
    temperature: Optional[float] = Form(None),
    api_key_record: Any = Depends(_impl.validate_api_key),
) -> Response:
    requested_model = model.strip()
    if not requested_model:
        raise HTTPException(status_code=400, detail="Missing model")

    audio = await file.read(MAX_AUDIO_BYTES + 1)
    if not audio:
        raise HTTPException(status_code=400, detail="Audio file is empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Audio file exceeds proxy limit of {MAX_AUDIO_BYTES} bytes",
        )

    endpoints = resolve_audio_endpoints(requested_model)
    last_error = ""
    started_at = datetime.utcnow().isoformat()
    started_monotonic = time.monotonic()

    for endpoint in endpoints:
        provider_name = str(endpoint.get("name") or "unknown")
        provider_model = str(endpoint.get("model") or requested_model)
        target_url = resolve_audio_transcription_url(endpoint)
        data: dict[str, str] = {"model": provider_model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        if response_format:
            data["response_format"] = response_format
        if temperature is not None:
            data["temperature"] = str(temperature)

        files = {
            "file": (
                file.filename or "audio.webm",
                audio,
                file.content_type or "application/octet-stream",
            )
        }

        try:
            if _impl.http_client is None:
                raise HTTPException(status_code=500, detail="HTTP client is not initialized")
            response = await _impl.http_client.post(
                target_url,
                data=data,
                files=files,
                headers=transcription_headers(endpoint),
                timeout=AUDIO_TIMEOUT_SECONDS,
            )
            body = response.content
            content_type = response.headers.get("content-type", "application/json")
            text = body.decode("utf-8", errors="replace")

            if 200 <= response.status_code < 300:
                _safe_insert_log(
                    api_key_record,
                    requested_model,
                    provider_name,
                    provider_model,
                    response.status_code,
                    started_at,
                    started_monotonic,
                    file.filename or "audio.webm",
                    output=text[:12000],
                )
                return Response(content=body, status_code=response.status_code, media_type=content_type)

            last_error = f"{provider_name} returned {response.status_code}: {text}"
            if response.status_code not in FALLBACK_STATUSES:
                _safe_insert_log(
                    api_key_record,
                    requested_model,
                    provider_name,
                    provider_model,
                    response.status_code,
                    started_at,
                    started_monotonic,
                    file.filename or "audio.webm",
                    error=text[:12000],
                )
                return Response(content=body, status_code=response.status_code, media_type=content_type)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_error = f"{provider_name} failed: {exc}"
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - provider isolation/fallback
            last_error = f"{provider_name} unexpected error: {exc}"

    last_endpoint = endpoints[-1]
    _safe_insert_log(
        api_key_record,
        requested_model,
        str(last_endpoint.get("name") or "unknown"),
        str(last_endpoint.get("model") or requested_model),
        502,
        started_at,
        started_monotonic,
        file.filename or "audio.webm",
        error=last_error,
    )
    raise HTTPException(status_code=502, detail=last_error or "All transcription providers failed")
