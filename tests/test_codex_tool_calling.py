import json

import main


def test_chat_tools_are_converted_to_responses_format():
    payload = {
        "messages": [{"role": "user", "content": "Use ping"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Test tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        "tool_choice": {"type": "function", "function": {"name": "ping"}},
        "parallel_tool_calls": False,
    }
    converted = main.chat_to_responses_payload(payload, "gpt-5.5")
    assert converted["tools"] == [{
        "type": "function",
        "name": "ping",
        "description": "Test tool",
        "parameters": {"type": "object", "properties": {}},
    }]
    assert converted["tool_choice"] == {"type": "function", "name": "ping"}
    assert converted["parallel_tool_calls"] is False


def test_tool_result_round_trip_is_converted():
    payload = {
        "messages": [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_123",
                "type": "function",
                "function": {"name": "ping", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_123", "content": "pong"},
        ]
    }
    converted = main.chat_to_responses_payload(payload, "gpt-5.5")
    assert converted["input"][0] == {
        "type": "function_call",
        "call_id": "call_123",
        "name": "ping",
        "arguments": "{}",
    }
    assert converted["input"][1] == {
        "type": "function_call_output",
        "call_id": "call_123",
        "output": "pong",
    }


def test_responses_function_call_becomes_chat_tool_call():
    response = {
        "output": [{
            "type": "function_call",
            "call_id": "call_123",
            "name": "ping",
            "arguments": "{}",
        }]
    }
    encoded = main.extract_response_text(response)
    completion = main.chat_completion_from_text("gpt-5.5", encoded)
    choice = completion["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"] == {"name": "ping", "arguments": "{}"}


def test_responses_sse_function_call_is_parsed():
    event = {
        "response": {
            "output": [{
                "type": "function_call",
                "call_id": "call_abc",
                "name": "ping",
                "arguments": json.dumps({}),
            }]
        },
    }
    body = (
        "event: response.completed\n"
        f"data: {json.dumps(event)}\n\n"
        "data: [DONE]\n\n"
    )
    encoded = main.extract_response_text_from_sse(body)
    completion = main.chat_completion_from_text("gpt-5.5", encoded)
    assert completion["choices"][0]["message"]["tool_calls"][0]["id"] == "call_abc"


def test_responses_sse_output_item_event_is_parsed():
    event = {
        "item": {
            "type": "function_call",
            "call_id": "call_item",
            "name": "ping",
            "arguments": "{}",
        }
    }
    body = (
        "event: response.output_item.done\n"
        f"data: {json.dumps(event)}\n\n"
        "data: [DONE]\n\n"
    )
    encoded = main.extract_response_text_from_sse(body)
    completion = main.chat_completion_from_text("gpt-5.5", encoded)
    assert completion["choices"][0]["message"]["tool_calls"][0]["id"] == "call_item"


def test_responses_sse_uses_output_item_when_completed_output_is_empty():
    output_item = {
        "type": "response.output_item.done",
        "item": {
            "id": "fc_sanitized",
            "type": "function_call",
            "status": "completed",
            "arguments": "{}",
            "call_id": "call_real_shape",
            "name": "ping",
        },
        "output_index": 0,
        "sequence_number": 5,
    }
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp_sanitized",
            "object": "response",
            "status": "completed",
            "output": [],
        },
        "sequence_number": 6,
    }
    body = (
        "event: response.output_item.done\n"
        f"data: {json.dumps(output_item)}\n\n"
        "event: response.completed\n"
        f"data: {json.dumps(completed)}\n\n"
    )

    encoded = main.extract_response_text_from_sse(body)
    completion = main.chat_completion_from_text("gpt-5.5", encoded)
    choice = completion["choices"][0]

    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [{
        "id": "call_real_shape",
        "type": "function",
        "function": {"name": "ping", "arguments": "{}"},
    }]
