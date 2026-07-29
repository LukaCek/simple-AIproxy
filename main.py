import json
import time
import uuid
from typing import Any, Dict, List, Optional

import main_impl as _impl
from main_impl import *  # noqa: F401,F403

_TOOL_SENTINEL = "__AIPROXY_TOOL_CALLS__"
_original_extract_response_text = _impl.extract_response_text
_original_extract_response_text_from_sse = _impl.extract_response_text_from_sse
_original_chat_completion_from_text = _impl.chat_completion_from_text
_original_sse_chat_chunks = _impl.sse_chat_chunks


def _response_tool_calls(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    calls: List[Dict[str, Any]] = []
    for item in data.get("output", []) if isinstance(data.get("output"), list) else []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        arguments = item.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        calls.append({
            "id": str(item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex)),
            "type": "function",
            "function": {
                "name": str(item.get("name") or ""),
                "arguments": arguments,
            },
        })
    return calls


def _encode_tool_calls(calls: List[Dict[str, Any]]) -> str:
    return _TOOL_SENTINEL + json.dumps(calls, ensure_ascii=False)


def _decode_tool_calls(text: str) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(text, str) or not text.startswith(_TOOL_SENTINEL):
        return None
    try:
        value = json.loads(text[len(_TOOL_SENTINEL):])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, list) else None


def extract_response_text(data: Any) -> str:
    calls = _response_tool_calls(data)
    if calls:
        return _encode_tool_calls(calls)
    return _original_extract_response_text(data)


def extract_response_text_from_sse(body: bytes | str) -> str:
    text_body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    completed_response: Optional[Dict[str, Any]] = None
    output_items: List[Dict[str, Any]] = []
    for line in text_body.splitlines():
        if not line.startswith("data:"):
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
        if event.get("type") == "response.completed" and isinstance(event.get("response"), dict):
            completed_response = event["response"]
        if event.get("type") in {"response.output_item.added", "response.output_item.done"} and isinstance(event.get("item"), dict):
            item = event["item"]
            if item not in output_items:
                output_items.append(item)
    calls = _response_tool_calls(completed_response or {"output": output_items})
    if calls:
        return _encode_tool_calls(calls)
    return _original_extract_response_text_from_sse(body)


def chat_completion_from_text(model: str, text: str) -> Dict[str, Any]:
    calls = _decode_tool_calls(text)
    if calls is None:
        return _original_chat_completion_from_text(model, text)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": calls},
            "finish_reason": "tool_calls",
        }],
    }


def sse_chat_chunks(model: str, text: str):
    calls = _decode_tool_calls(text)
    if calls is None:
        yield from _original_sse_chat_chunks(model, text)
        return
    chunk_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    first = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
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
        }],
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode("utf-8")
    end = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    }
    yield f"data: {json.dumps(end, ensure_ascii=False)}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def chat_to_responses_payload(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
    messages = payload.get("messages", [])
    input_items: List[Dict[str, Any]] = []
    instructions: Optional[str] = str(payload.get("instructions")) if payload.get("instructions") else None

    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if role == "system":
            system_text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            instructions = (instructions + "\n" if instructions else "") + system_text
            continue
        if role == "tool":
            output = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            input_items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": output,
            })
            continue
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            if content:
                input_items.append({"role": "assistant", "content": content})
            for tool_call in message["tool_calls"]:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                arguments = function.get("arguments", "{}")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                input_items.append({
                    "type": "function_call",
                    "call_id": str(tool_call.get("id") or ("call_" + uuid.uuid4().hex)),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                })
            continue
        input_items.append({"role": role, "content": content})

    converted: Dict[str, Any] = {
        "model": model,
        "input": input_items or str(payload.get("prompt", "")),
        "instructions": instructions or "You are a helpful assistant.",
        "store": bool(payload.get("store")) if payload.get("store") is not None else False,
        "stream": True,
    }

    converted_tools: List[Dict[str, Any]] = []
    for tool in payload.get("tools", []) if isinstance(payload.get("tools"), list) else []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        converted_tool: Dict[str, Any] = {
            "type": "function",
            "name": str(function.get("name") or ""),
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        }
        if function.get("description") is not None:
            converted_tool["description"] = function.get("description")
        if function.get("strict") is not None:
            converted_tool["strict"] = bool(function.get("strict"))
        converted_tools.append(converted_tool)
    if converted_tools:
        converted["tools"] = converted_tools

    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, str):
        converted["tool_choice"] = tool_choice
    elif isinstance(tool_choice, dict):
        function = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else {}
        if tool_choice.get("type") == "function" and function.get("name"):
            converted["tool_choice"] = {"type": "function", "name": str(function["name"])}
    if payload.get("parallel_tool_calls") is not None:
        converted["parallel_tool_calls"] = bool(payload.get("parallel_tool_calls"))
    if payload.get("temperature") is not None:
        converted["temperature"] = payload.get("temperature")
    max_output_tokens = payload.get("max_completion_tokens", payload.get("max_tokens"))
    if max_output_tokens is not None:
        converted["max_output_tokens"] = max_output_tokens
    return converted


# Route handlers in main_impl resolve these names from main_impl's module globals.
_impl.extract_response_text = extract_response_text
_impl.extract_response_text_from_sse = extract_response_text_from_sse
_impl.chat_completion_from_text = chat_completion_from_text
_impl.sse_chat_chunks = sse_chat_chunks
_impl.chat_to_responses_payload = chat_to_responses_payload

app = _impl.app
