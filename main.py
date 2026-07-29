import json
import sys
import time
import uuid
from typing import Any

import main_impl as _impl

_TOOL_SENTINEL = "__AIPROXY_TOOL_CALLS__"
_original_extract_response_text = _impl.extract_response_text
_original_extract_response_text_from_sse = _impl.extract_response_text_from_sse
_original_chat_completion_from_text = _impl.chat_completion_from_text
_original_sse_chat_chunks = _impl.sse_chat_chunks


def _response_tool_calls(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []

    calls: list[dict[str, Any]] = []
    output = data.get("output")
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue

        arguments = item.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)

        calls.append(
            {
                "id": str(
                    item.get("call_id")
                    or item.get("id")
                    or f"call_{uuid.uuid4().hex}"
                ),
                "type": "function",
                "function": {
                    "name": str(item.get("name") or ""),
                    "arguments": arguments,
                },
            }
        )
    return calls


def _encode_tool_calls(calls: list[dict[str, Any]]) -> str:
    return _TOOL_SENTINEL + json.dumps(calls, ensure_ascii=False)


def _decode_tool_calls(text: str) -> list[dict[str, Any]] | None:
    if not text.startswith(_TOOL_SENTINEL):
        return None

    try:
        value = json.loads(text[len(_TOOL_SENTINEL) :])
    except json.JSONDecodeError:
        return None

    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return None
    return value


def extract_response_text(data: Any) -> str:
    calls = _response_tool_calls(data)
    if calls:
        return _encode_tool_calls(calls)
    return _original_extract_response_text(data)


def extract_response_text_from_sse(body: bytes | str) -> str:
    text_body = (
        body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    )
    completed_response: dict[str, Any] | None = None
    output_items: dict[str, dict[str, Any]] = {}
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
        response = event.get("response")
        if event_type == "response.completed" and isinstance(response, dict):
            completed_response = response

        item = event.get("item")
        if (
            event_type
            in {"response.output_item.added", "response.output_item.done"}
            and isinstance(item, dict)
        ):
            item_key = str(
                item.get("call_id")
                or item.get("id")
                or f"output_{event.get('output_index', len(output_items))}"
            )
            output_items[item_key] = item

    calls = _response_tool_calls(completed_response)
    if not calls:
        calls = _response_tool_calls({"output": list(output_items.values())})
    if calls:
        return _encode_tool_calls(calls)
    return _original_extract_response_text_from_sse(body)


def chat_completion_from_text(model: str, text: str) -> dict[str, Any]:
    calls = _decode_tool_calls(text)
    if calls is None:
        return _original_chat_completion_from_text(model, text)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
    }


def sse_chat_chunks(model: str, text: str):
    calls = _decode_tool_calls(text)
    if calls is None:
        yield from _original_sse_chat_chunks(model, text)
        return

    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    first = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call["id"],
                            "type": "function",
                            "function": call["function"],
                        }
                        for index, call in enumerate(calls)
                    ],
                },
                "finish_reason": None,
            }
        ],
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode()

    end = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    yield f"data: {json.dumps(end, ensure_ascii=False)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def chat_to_responses_payload(
    payload: dict[str, Any], model: str
) -> dict[str, Any]:
    messages = payload.get("messages", [])
    input_items: list[dict[str, Any]] = []
    instructions = (
        str(payload["instructions"]) if payload.get("instructions") else None
    )

    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue

        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if role == "system":
            system_text = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            instructions = f"{instructions}\n{system_text}" if instructions else system_text
            continue

        if role == "tool":
            output = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(message.get("tool_call_id") or ""),
                    "output": output,
                }
            )
            continue

        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list):
            if content:
                input_items.append({"role": "assistant", "content": content})

            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                raw_function = tool_call.get("function")
                function = raw_function if isinstance(raw_function, dict) else {}
                arguments = function.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": str(
                            tool_call.get("id") or f"call_{uuid.uuid4().hex}"
                        ),
                        "name": str(function.get("name") or ""),
                        "arguments": arguments,
                    }
                )
            continue

        input_items.append({"role": role, "content": content})

    converted: dict[str, Any] = {
        "model": model,
        "input": input_items or str(payload.get("prompt", "")),
        "instructions": instructions or "You are a helpful assistant.",
        "store": bool(payload.get("store")) if payload.get("store") is not None else False,
        "stream": True,
    }

    converted_tools: list[dict[str, Any]] = []
    raw_tools = payload.get("tools")
    for tool in raw_tools if isinstance(raw_tools, list) else []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue

        raw_function = tool.get("function")
        function = raw_function if isinstance(raw_function, dict) else tool
        converted_tool: dict[str, Any] = {
            "type": "function",
            "name": str(function.get("name") or ""),
            "parameters": function.get("parameters")
            or {"type": "object", "properties": {}},
        }
        if function.get("description") is not None:
            converted_tool["description"] = function["description"]
        if function.get("strict") is not None:
            converted_tool["strict"] = bool(function["strict"])
        converted_tools.append(converted_tool)

    if converted_tools:
        converted["tools"] = converted_tools

    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, str):
        converted["tool_choice"] = tool_choice
    elif isinstance(tool_choice, dict):
        raw_function = tool_choice.get("function")
        function = raw_function if isinstance(raw_function, dict) else {}
        if tool_choice.get("type") == "function" and function.get("name"):
            converted["tool_choice"] = {
                "type": "function",
                "name": str(function["name"]),
            }

    if payload.get("parallel_tool_calls") is not None:
        converted["parallel_tool_calls"] = bool(payload["parallel_tool_calls"])
    if payload.get("temperature") is not None:
        converted["temperature"] = payload["temperature"]

    return converted


# Route handlers in main_impl resolve these names from main_impl's module globals.
_impl.extract_response_text = extract_response_text
_impl.extract_response_text_from_sse = extract_response_text_from_sse
_impl.chat_completion_from_text = chat_completion_from_text
_impl.sse_chat_chunks = sse_chat_chunks
_impl.chat_to_responses_payload = chat_to_responses_payload

# Expose the implementation module as `main` so existing monkeypatch-based tests and
# runtime configuration updates modify the exact globals used by FastAPI handlers.
sys.modules[__name__] = _impl
