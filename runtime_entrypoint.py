"""Runtime extensions for Codex Responses compatibility."""

from __future__ import annotations

import json
from typing import Any

# Import the base compatibility module only. Selectable Codex model support is
# composed separately by full_entrypoint.py so importing this module in tests
# does not mutate provider/model configuration as a collection-time side effect.
import main as _impl

_original_extract_response_text_from_sse = _impl.extract_response_text_from_sse
_original_chat_to_responses_payload = _impl.chat_to_responses_payload


def chat_to_responses_payload(payload: dict[str, Any], model: str) -> dict[str, Any]:
    """Convert Chat Completions input while omitting unsupported Codex fields.

    Home Assistant includes ``temperature`` in its OpenAI-compatible requests.
    The ChatGPT Codex Responses backend rejects that field, so it must not be
    forwarded. Other provider modes still receive the original request payload.
    """
    converted = _original_chat_to_responses_payload(payload, model)
    converted.pop("temperature", None)
    return converted


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("value")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("value")
        return text if isinstance(text, str) else ""
    return ""


def _text_from_response(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    parts: list[str] = []
    output = response.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        text = _text_from_content(item.get("content"))
        if text:
            parts.append(text)
    return "".join(parts)


def extract_response_text_from_sse(body: bytes | str) -> str:
    """Extract normal Responses text without leaking the raw SSE transcript.

    The existing compatibility layer already handles function calls. This wrapper
    preserves that result, then handles Codex text delta/snapshot event shapes
    that otherwise fall through as the complete ``event:/data:`` stream.
    """
    original = _original_extract_response_text_from_sse(body)
    if original.startswith("__AIPROXY_TOOL_CALLS__"):
        return original

    text_body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    deltas: list[str] = []
    snapshots: dict[str, dict[str, Any]] = {}
    completed_response: dict[str, Any] | None = None
    current_event = ""

    for raw_line in text_body.splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            current_event = line[6:].strip()
            continue
        if not line.startswith("data:"):
            if not line:
                current_event = ""
            continue

        data_text = line[5:].strip()
        if not data_text or data_text == "[DONE]":
            continue
        try:
            event = json.loads(data_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = str(event.get("type") or current_event or "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue

        if event_type == "response.content_part.delta":
            delta = event.get("delta")
            text = _text_from_content(delta)
            if text:
                deltas.append(text)
            continue

        item = event.get("item")
        if event_type in {"response.output_item.added", "response.output_item.done"} and isinstance(item, dict):
            key = str(item.get("id") or f"output_{event.get('output_index', len(snapshots))}")
            snapshots[key] = item

        response = event.get("response")
        if event_type == "response.completed" and isinstance(response, dict):
            completed_response = response

    if deltas:
        return "".join(deltas)

    completed_text = _text_from_response(completed_response)
    if completed_text:
        return completed_text

    snapshot_text = _text_from_response({"output": list(snapshots.values())})
    if snapshot_text:
        return snapshot_text

    return original


_impl.chat_to_responses_payload = chat_to_responses_payload
_impl.extract_response_text_from_sse = extract_response_text_from_sse
app = _impl.app
