"""True streaming adapter for Responses/Codex providers.

The base proxy historically buffered the complete Responses API body and only
then converted it to Chat Completions SSE. Long Codex generations therefore
looked idle to clients and reverse proxies. This extension replaces the public
streaming chat-completions path while keeping non-streaming/background requests
on the original implementation.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Optional

import httpx
from fastapi import BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

# Apply the existing payload, text-SSE, tool-call, and selectable-model patches
# before installing the replacement route.
import runtime_entrypoint as _runtime  # noqa: F401
import codex_entrypoint as _codex
import main_impl as _impl

app = _codex.app

if not hasattr(_impl, "_responses_streaming_original_chat_completions"):
    _impl._responses_streaming_original_chat_completions = _impl.chat_completions
_original_chat_completions = _impl._responses_streaming_original_chat_completions

_FALLBACK_STATUSES = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}


def _chat_chunk(
    model: str,
    chunk_id: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: Optional[str] = None,
) -> bytes:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _sse_event(raw_event: str) -> tuple[str, Optional[dict[str, Any]]]:
    event_name = ""
    data_lines: list[str] = []
    for raw_line in raw_event.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return event_name, None
    data_text = "\n".join(data_lines).strip()
    if not data_text or data_text == "[DONE]":
        return event_name, None
    try:
        value = json.loads(data_text)
    except json.JSONDecodeError:
        return event_name, None
    return event_name, value if isinstance(value, dict) else None


def _pop_sse_events(buffer: str) -> tuple[list[str], str]:
    events: list[str] = []
    while True:
        lf = buffer.find("\n\n")
        crlf = buffer.find("\r\n\r\n")
        delimiters = [(lf, 2), (crlf, 4)]
        delimiters = [(index, size) for index, size in delimiters if index >= 0]
        if not delimiters:
            break
        index, size = min(delimiters, key=lambda item: item[0])
        events.append(buffer[:index])
        buffer = buffer[index + size :]
    return events, buffer


def _content_part_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "value", "delta"):
            text = value.get(key)
            if isinstance(text, str):
                return text
    return ""


async def responses_to_chat_sse(
    response: httpx.Response,
    model: str,
    *,
    heartbeat_seconds: float = 15.0,
    on_first: Optional[Callable[[], None]] = None,
    on_upstream_bytes: Optional[Callable[[bytes], None]] = None,
    on_text: Optional[Callable[[str], None]] = None,
) -> AsyncIterator[bytes]:
    """Translate Responses SSE to Chat Completions SSE incrementally.

    The assistant-role chunk is emitted as soon as upstream response headers are
    available. If the Responses stream is silent while the model is reasoning,
    SSE comments are emitted periodically without cancelling the in-flight read.
    These comments are ignored by OpenAI-compatible clients but keep reverse
    proxies from treating the connection as idle.
    """

    chunk_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    if on_first is not None:
        on_first()
    yield _chat_chunk(model, chunk_id, created, {"role": "assistant"})

    text_buffer = ""
    tool_states: dict[str, dict[str, Any]] = {}
    next_tool_index = 0
    saw_tool_call = False
    upstream_iter = response.aiter_bytes().__aiter__()
    pending: Optional[asyncio.Task[bytes]] = asyncio.create_task(anext(upstream_iter))

    def tool_key(event: dict[str, Any], item: Optional[dict[str, Any]] = None) -> str:
        source = item or {}
        return str(
            event.get("item_id")
            or source.get("id")
            or source.get("call_id")
            or f"output_{event.get('output_index', 0)}"
        )

    def get_tool_state(
        event: dict[str, Any], item: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        nonlocal next_tool_index
        key = tool_key(event, item)
        state = tool_states.get(key)
        if state is None:
            index_value = event.get("output_index")
            index = int(index_value) if isinstance(index_value, int) else next_tool_index
            state = {
                "index": index,
                "started": False,
                "arguments_streamed": False,
                "id": "",
                "name": "",
            }
            next_tool_index = max(next_tool_index, index + 1)
            tool_states[key] = state
        if item:
            state["id"] = str(
                item.get("call_id")
                or item.get("id")
                or state["id"]
                or f"call_{uuid.uuid4().hex}"
            )
            state["name"] = str(item.get("name") or state["name"] or "")
        return state

    def tool_start_delta(state: dict[str, Any]) -> dict[str, Any]:
        state["started"] = True
        if not state["id"]:
            state["id"] = f"call_{uuid.uuid4().hex}"
        return {
            "tool_calls": [
                {
                    "index": state["index"],
                    "id": state["id"],
                    "type": "function",
                    "function": {"name": state["name"], "arguments": ""},
                }
            ]
        }

    async def handle_event(raw_event: str) -> AsyncIterator[bytes]:
        nonlocal saw_tool_call
        event_name, event = _sse_event(raw_event)
        if event is None:
            return
        event_type = str(event.get("type") or event_name or "")

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                if on_text is not None:
                    on_text(delta)
                yield _chat_chunk(model, chunk_id, created, {"content": delta})
            return

        if event_type == "response.content_part.delta":
            delta = _content_part_text(event.get("delta"))
            if delta:
                if on_text is not None:
                    on_text(delta)
                yield _chat_chunk(model, chunk_id, created, {"content": delta})
            return

        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if (
            event_type == "response.output_item.added"
            and item
            and item.get("type") == "function_call"
        ):
            saw_tool_call = True
            state = get_tool_state(event, item)
            if not state["started"]:
                yield _chat_chunk(model, chunk_id, created, tool_start_delta(state))
            return

        if event_type == "response.function_call_arguments.delta":
            saw_tool_call = True
            state = get_tool_state(event)
            delta = event.get("delta")
            if not state["started"]:
                yield _chat_chunk(model, chunk_id, created, tool_start_delta(state))
            if isinstance(delta, str) and delta:
                state["arguments_streamed"] = True
                yield _chat_chunk(
                    model,
                    chunk_id,
                    created,
                    {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "function": {"arguments": delta},
                            }
                        ]
                    },
                )
            return

        if (
            event_type == "response.output_item.done"
            and item
            and item.get("type") == "function_call"
        ):
            saw_tool_call = True
            state = get_tool_state(event, item)
            if not state["started"]:
                yield _chat_chunk(model, chunk_id, created, tool_start_delta(state))
            arguments = item.get("arguments")
            if (
                isinstance(arguments, str)
                and arguments
                and not state["arguments_streamed"]
            ):
                yield _chat_chunk(
                    model,
                    chunk_id,
                    created,
                    {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "function": {"arguments": arguments},
                            }
                        ]
                    },
                )
            return

    try:
        while pending is not None:
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                yield b": aiproxy keep-alive\n\n"
                continue
            try:
                chunk = pending.result()
            except StopAsyncIteration:
                pending = None
                break

            pending = asyncio.create_task(anext(upstream_iter))
            if not chunk:
                continue
            if on_upstream_bytes is not None:
                on_upstream_bytes(chunk)
            text_buffer += chunk.decode("utf-8", errors="replace")
            events, text_buffer = _pop_sse_events(text_buffer)
            for raw_event in events:
                async for converted in handle_event(raw_event):
                    yield converted

        if text_buffer.strip():
            async for converted in handle_event(text_buffer):
                yield converted

        finish_reason = "tool_calls" if saw_tool_call else "stop"
        yield _chat_chunk(model, chunk_id, created, {}, finish_reason=finish_reason)
        yield b"data: [DONE]\n\n"
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


async def _passthrough_stream(
    response: httpx.Response,
    *,
    on_first: Callable[[], None],
    on_upstream_bytes: Callable[[bytes], None],
) -> AsyncIterator[bytes]:
    marked = False
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        if not marked:
            marked = True
            on_first()
        on_upstream_bytes(chunk)
        yield chunk


async def chat_completions(
    request: Request,
    background_tasks: BackgroundTasks,
    api_key_record: sqlite3.Row = Depends(_impl.validate_api_key),
) -> Response:
    payload = await request.json()

    # Only replace the path that needs incremental delivery. Everything else
    # remains on the established implementation.
    if payload.get("stream") is not True or payload.get("background") is True:
        return await _original_chat_completions(request, background_tasks, api_key_record)

    requested_model = str(payload.get("model") or "")
    if not requested_model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing model in request payload",
        )

    endpoints = _impl.resolve_requested_model(requested_model)
    api_key_value = api_key_record["key"]
    api_key_name = api_key_record["name"] if "name" in api_key_record.keys() else None
    prompt_text = _impl.extract_prompt(payload)
    started_monotonic = time.monotonic()
    started_at = datetime.utcnow().isoformat()
    last_error: Optional[str] = None

    def timing(first_at: Optional[str] = None) -> tuple[str, Optional[float], float]:
        ended_at = datetime.utcnow().isoformat()
        total_ms = (time.monotonic() - started_monotonic) * 1000
        first_ms: Optional[float] = None
        if first_at:
            try:
                first_ms = (
                    datetime.fromisoformat(first_at) - datetime.fromisoformat(started_at)
                ).total_seconds() * 1000
            except Exception:
                first_ms = None
        return ended_at, first_ms, total_ms

    for endpoint in endpoints:
        provider_name = str(endpoint.get("name", "unknown"))
        provider_model = str(endpoint.get("model") or "")
        provider_payload = _impl.prepare_provider_chat_payload(
            payload, endpoint, provider_model
        )
        response: Optional[httpx.Response] = None

        try:
            await _impl.ensure_provider_token(endpoint)
            if _impl.http_client is None:
                raise RuntimeError("HTTP client is not initialized")

            api_mode = str(endpoint.get("api_mode", "openai_chat_completions"))
            responses_mode = api_mode in {"openai_responses", "codex_responses"}
            if responses_mode:
                upstream_payload = _impl.chat_to_responses_payload(
                    provider_payload, provider_model
                )
                target_url = _impl.resolve_responses_url(endpoint)
            else:
                upstream_payload = provider_payload
                target_url = _impl.resolve_endpoint_url(endpoint)

            request_obj = _impl.http_client.build_request(
                "POST",
                target_url,
                json=upstream_payload,
                headers=_impl.build_provider_headers(endpoint),
            )
            response = await _impl.http_client.send(request_obj, stream=True)
            upstream_status = response.status_code
            content_type = response.headers.get("content-type", "application/json")

            # Before returning a StreamingResponse we can still inspect failures,
            # refresh auth state, or fall back to the next configured provider.
            if upstream_status != 200:
                content = await response.aread()
                await response.aclose()
                response = None
                raw_text = content.decode("utf-8", errors="replace")
                first_response_at = datetime.utcnow().isoformat()

                if (
                    api_mode == "codex_responses"
                    and _impl.response_indicates_token_expired(upstream_status, raw_text)
                ):
                    error_msg = _impl.codex_reauth_message(provider_name)
                    _impl.mark_provider_reauth_required(provider_name, error_msg)
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    _impl.insert_log(
                        api_key_value,
                        api_key_name,
                        requested_model,
                        requested_model,
                        provider_name,
                        provider_model,
                        upstream_status,
                        started_at,
                        first_response_at,
                        ended_at,
                        first_ms,
                        total_ms,
                        prompt_text,
                        None,
                        error_msg,
                    )
                    last_error = error_msg
                    continue

                if _impl.looks_like_html_response(raw_text, content_type):
                    error_msg = _impl.provider_html_error(api_mode, raw_text)
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    _impl.insert_log(
                        api_key_value,
                        api_key_name,
                        requested_model,
                        requested_model,
                        provider_name,
                        provider_model,
                        502,
                        started_at,
                        first_response_at,
                        ended_at,
                        first_ms,
                        total_ms,
                        prompt_text,
                        None,
                        error_msg,
                    )
                    last_error = f"{provider_name} returned HTML challenge: {error_msg}"
                    continue

                error_msg = raw_text
                if upstream_status in _FALLBACK_STATUSES:
                    last_error = (
                        f"{provider_name} returned {upstream_status}: {error_msg}"
                    )
                    continue

                ended_at, first_ms, total_ms = timing(first_response_at)
                _impl.insert_log(
                    api_key_value,
                    api_key_name,
                    requested_model,
                    requested_model,
                    provider_name,
                    provider_model,
                    upstream_status,
                    started_at,
                    first_response_at,
                    ended_at,
                    first_ms,
                    total_ms,
                    prompt_text,
                    None,
                    error_msg,
                )
                return Response(
                    content,
                    status_code=upstream_status,
                    media_type=content_type,
                )

            # A 200 HTML challenge is also an error, not a successful stream.
            if "text/html" in content_type.lower():
                content = await response.aread()
                await response.aclose()
                response = None
                raw_text = content.decode("utf-8", errors="replace")
                first_response_at = datetime.utcnow().isoformat()
                error_msg = _impl.provider_html_error(api_mode, raw_text)
                ended_at, first_ms, total_ms = timing(first_response_at)
                _impl.insert_log(
                    api_key_value,
                    api_key_name,
                    requested_model,
                    requested_model,
                    provider_name,
                    provider_model,
                    502,
                    started_at,
                    first_response_at,
                    ended_at,
                    first_ms,
                    total_ms,
                    prompt_text,
                    None,
                    error_msg,
                )
                last_error = f"{provider_name} returned HTML challenge: {error_msg}"
                continue

            first_response_at: Optional[str] = None
            captured = bytearray()
            text_parts: list[str] = []
            stream_error: Optional[str] = None

            def mark_first() -> None:
                nonlocal first_response_at
                if first_response_at is None:
                    first_response_at = datetime.utcnow().isoformat()

            def capture(chunk: bytes) -> None:
                if len(captured) < 12000:
                    captured.extend(chunk[: 12000 - len(captured)])

            def capture_text(text: str) -> None:
                text_parts.append(text)

            async def body() -> AsyncIterator[bytes]:
                nonlocal stream_error
                try:
                    if responses_mode:
                        async for out in responses_to_chat_sse(
                            response,
                            provider_model,
                            on_first=mark_first,
                            on_upstream_bytes=capture,
                            on_text=capture_text,
                        ):
                            yield out
                    else:
                        async for out in _passthrough_stream(
                            response,
                            on_first=mark_first,
                            on_upstream_bytes=capture,
                        ):
                            yield out
                except Exception as exc:
                    stream_error = _impl.format_provider_exception(exc)
                    raise
                finally:
                    await response.aclose()
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    if text_parts:
                        output_text = "".join(text_parts)
                    elif captured:
                        if responses_mode:
                            output_text = _impl.extract_response_text_from_sse(
                                bytes(captured)
                            )
                        else:
                            output_text = _impl.extract_output_from_body(
                                bytes(captured), content_type
                            )
                    else:
                        output_text = ""
                    _impl.insert_log(
                        api_key_value,
                        api_key_name,
                        requested_model,
                        requested_model,
                        provider_name,
                        provider_model,
                        200 if stream_error is None else 502,
                        started_at,
                        first_response_at,
                        ended_at,
                        first_ms,
                        total_ms,
                        prompt_text,
                        output_text,
                        stream_error,
                    )

            media_type = "text/event-stream" if responses_mode else content_type
            return StreamingResponse(
                body(),
                status_code=200,
                media_type=media_type,
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
            )

        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            if response is not None:
                await response.aclose()
            last_error = f"{provider_name} failed: {_impl.format_provider_exception(exc)}"
            continue
        except Exception as exc:
            if response is not None:
                await response.aclose()
            last_error = f"{provider_name} unexpected error: {exc}"
            continue

    ended_at, first_ms, total_ms = timing(None)
    last_endpoint = endpoints[-1] if endpoints else {}
    _impl.insert_log(
        api_key_value,
        api_key_name,
        requested_model,
        requested_model,
        str(last_endpoint.get("name", "unknown")),
        str(last_endpoint.get("model", "unknown")),
        502,
        started_at,
        None,
        ended_at,
        first_ms,
        total_ms,
        prompt_text,
        None,
        last_error,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=last_error or "All provider endpoints failed",
    )


def _install_route() -> None:
    # FastAPI stores the endpoint object in APIRoute, so replacing the module
    # symbol alone does not affect an already-registered route.
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (
            getattr(route, "path", None) == "/v1/chat/completions"
            and "POST" in (getattr(route, "methods", None) or set())
        )
    ]
    app.add_api_route(
        "/v1/chat/completions",
        chat_completions,
        methods=["POST"],
        response_class=Response,
    )


_install_route()
