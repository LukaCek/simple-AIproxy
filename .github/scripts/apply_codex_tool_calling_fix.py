from pathlib import Path

MAIN = Path("main.py")
TEST = Path("tests/test_codex_tool_calling.py")
text = MAIN.read_text(encoding="utf-8")

old_chat_completion = '''def chat_completion_from_text(model: str, text: str) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
'''

new_chat_completion = '''def chat_completion_from_text(model: str, text: str) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


def parse_responses_body(content: bytes | str) -> Dict[str, Any]:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"output": []}
    except json.JSONDecodeError:
        pass

    completed_response: Optional[Dict[str, Any]] = None
    output_items: List[Dict[str, Any]] = []
    text_deltas: List[str] = []
    for line in text.splitlines():
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
        event_type = str(event.get("type") or "")
        response = event.get("response")
        if event_type == "response.completed" and isinstance(response, dict):
            completed_response = response
        item = event.get("item")
        if event_type in {"response.output_item.done", "response.output_item.added"} and isinstance(item, dict):
            if item not in output_items:
                output_items.append(item)
        delta = event.get("delta")
        if event_type == "response.output_text.delta" and isinstance(delta, str):
            text_deltas.append(delta)

    if completed_response is not None:
        return completed_response
    result: Dict[str, Any] = {"output": output_items}
    if text_deltas:
        result["output_text"] = "".join(text_deltas)
    return result


def chat_completion_from_response(model: str, data: Dict[str, Any]) -> Dict[str, Any]:
    tool_calls: List[Dict[str, Any]] = []
    for item in data.get("output", []) if isinstance(data.get("output"), list) else []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        call_id = str(item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex))
        arguments = item.get("arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {
                "name": str(item.get("name") or ""),
                "arguments": arguments,
            },
        })

    text = extract_response_text(data)
    if text.startswith("{") and tool_calls:
        text = ""
    message: Dict[str, Any] = {"role": "assistant", "content": text or None}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        finish_reason = "tool_calls"
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }


def sse_chat_completion_chunks(completion: Dict[str, Any]):
    choice = completion["choices"][0]
    message = choice["message"]
    chunk_id = completion["id"]
    created = completion["created"]
    model = completion["model"]
    delta: Dict[str, Any] = {"role": "assistant"}
    if message.get("content") is not None:
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [
            {
                "index": index,
                "id": call["id"],
                "type": "function",
                "function": call["function"],
            }
            for index, call in enumerate(message["tool_calls"])
        ]
    body = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    yield f"data: {json.dumps(body, ensure_ascii=False)}\\n\\n".encode("utf-8")
    end = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
    }
    yield f"data: {json.dumps(end, ensure_ascii=False)}\\n\\n".encode("utf-8")
    yield b"data: [DONE]\\n\\n"
'''

old_converter_start = text.index("def chat_to_responses_payload(")
old_converter_end = text.index("\n\nasync def send_responses_adapter", old_converter_start)
new_converter = '''def chat_to_responses_payload(payload: Dict[str, Any], model: str) -> Dict[str, Any]:
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
            instructions = (instructions + "\\n" if instructions else "") + system_text
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

    converted: Dict[str, Any] = {"model": model, "input": input_items or str(payload.get("prompt", ""))}
    converted["instructions"] = instructions or "You are a helpful assistant."
    converted["store"] = bool(payload.get("store")) if payload.get("store") is not None else False
    converted["stream"] = True

    tools: List[Dict[str, Any]] = []
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
        tools.append(converted_tool)
    if tools:
        converted["tools"] = tools

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
'''

if old_chat_completion not in text:
    raise SystemExit("chat_completion_from_text block not found")
text = text.replace(old_chat_completion, new_chat_completion, 1)
text = text[:old_converter_start] + new_converter + text[old_converter_end:]

old_response_handling = '''                if response.status_code == 200:
                    try:
                        text = extract_response_text(response.json())
                    except Exception:
                        text = extract_response_text_from_sse(content) or raw_text
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, 200, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, text, None)
                    if payload.get("stream"):
                        return StreamingResponse(sse_chat_chunks(provider_model, text), status_code=200, media_type="text/event-stream")
                    return Response(json.dumps(chat_completion_from_text(provider_model, text), ensure_ascii=False), status_code=200, media_type="application/json")
'''
new_response_handling = '''                if response.status_code == 200:
                    parsed_response = parse_responses_body(content)
                    completion = chat_completion_from_response(provider_model, parsed_response)
                    message = completion["choices"][0]["message"]
                    log_output = message.get("content") or json.dumps(message.get("tool_calls") or [], ensure_ascii=False)
                    ended_at, first_ms, total_ms = timing(first_response_at)
                    insert_log(api_key_value, api_key_name, requested_model, requested_model, provider_name, provider_model, 200, started_at, first_response_at, ended_at, first_ms, total_ms, prompt_text, log_output, None)
                    if payload.get("stream"):
                        return StreamingResponse(sse_chat_completion_chunks(completion), status_code=200, media_type="text/event-stream")
                    return Response(json.dumps(completion, ensure_ascii=False), status_code=200, media_type="application/json")
'''
if old_response_handling not in text:
    raise SystemExit("Responses endpoint handling block not found")
text = text.replace(old_response_handling, new_response_handling, 1)
MAIN.write_text(text, encoding="utf-8")

TEST.parent.mkdir(parents=True, exist_ok=True)
TEST.write_text('''import json\n\nimport main\n\n\ndef test_chat_tools_are_converted_to_responses_format():\n    payload = {\n        "messages": [{"role": "user", "content": "Use ping"}],\n        "tools": [{\n            "type": "function",\n            "function": {\n                "name": "ping",\n                "description": "Test tool",\n                "parameters": {"type": "object", "properties": {}},\n            },\n        }],\n        "tool_choice": {"type": "function", "function": {"name": "ping"}},\n        "parallel_tool_calls": False,\n    }\n    converted = main.chat_to_responses_payload(payload, "gpt-5.5")\n    assert converted["tools"] == [{\n        "type": "function",\n        "name": "ping",\n        "description": "Test tool",\n        "parameters": {"type": "object", "properties": {}},\n    }]\n    assert converted["tool_choice"] == {"type": "function", "name": "ping"}\n    assert converted["parallel_tool_calls"] is False\n\n\ndef test_tool_result_round_trip_is_converted():\n    payload = {\n        "messages": [\n            {"role": "assistant", "content": None, "tool_calls": [{\n                "id": "call_123",\n                "type": "function",\n                "function": {"name": "ping", "arguments": "{}"},\n            }]},\n            {"role": "tool", "tool_call_id": "call_123", "content": "pong"},\n        ]\n    }\n    converted = main.chat_to_responses_payload(payload, "gpt-5.5")\n    assert converted["input"][0] == {\n        "type": "function_call",\n        "call_id": "call_123",\n        "name": "ping",\n        "arguments": "{}",\n    }\n    assert converted["input"][1] == {\n        "type": "function_call_output",\n        "call_id": "call_123",\n        "output": "pong",\n    }\n\n\ndef test_responses_function_call_becomes_chat_tool_call():\n    response = {\n        "output": [{\n            "type": "function_call",\n            "call_id": "call_123",\n            "name": "ping",\n            "arguments": "{}",\n        }]\n    }\n    completion = main.chat_completion_from_response("gpt-5.5", response)\n    choice = completion["choices"][0]\n    assert choice["finish_reason"] == "tool_calls"\n    assert choice["message"]["content"] is None\n    assert choice["message"]["tool_calls"][0]["function"] == {"name": "ping", "arguments": "{}"}\n\n\ndef test_responses_sse_function_call_is_parsed():\n    event = {\n        "type": "response.completed",\n        "response": {\n            "output": [{\n                "type": "function_call",\n                "call_id": "call_abc",\n                "name": "ping",\n                "arguments": json.dumps({}),\n            }]\n        },\n    }\n    parsed = main.parse_responses_body(f"data: {json.dumps(event)}\\n\\ndata: [DONE]\\n\\n")\n    assert parsed["output"][0]["call_id"] == "call_abc"\n''', encoding="utf-8")

# Remove the one-shot machinery from the resulting commit.
Path(".github/workflows/apply-codex-tool-fix.yml").unlink(missing_ok=True)
Path(".github/scripts/apply_codex_tool_calling_fix.py").unlink(missing_ok=True)
