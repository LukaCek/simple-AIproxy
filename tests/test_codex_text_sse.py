import json

import runtime_entrypoint


def test_codex_output_text_deltas_do_not_return_raw_sse():
    events = [
        (
            "response.created",
            {
                "type": "response.created",
                "response": {"id": "resp_1", "status": "in_progress"},
            },
        ),
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "delta": "Use the ",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
            },
        ),
        (
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "delta": "repository tools.",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
            },
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {"id": "resp_1", "status": "completed", "output": []},
            },
        ),
    ]
    body = "".join(
        f"event: {event_name}\ndata: {json.dumps(event)}\n\n"
        for event_name, event in events
    )

    result = runtime_entrypoint.extract_response_text_from_sse(body)

    assert result == "Use the repository tools."
    assert not result.startswith("event:")


def test_codex_message_snapshot_is_used_when_delta_is_missing():
    output_item = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "id": "msg_1",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "Inspect main.py first."}
            ],
        },
    }
    completed = {
        "type": "response.completed",
        "response": {"id": "resp_1", "status": "completed", "output": []},
    }
    body = (
        "event: response.output_item.done\n"
        f"data: {json.dumps(output_item)}\n\n"
        "event: response.completed\n"
        f"data: {json.dumps(completed)}\n\n"
    )

    assert (
        runtime_entrypoint.extract_response_text_from_sse(body)
        == "Inspect main.py first."
    )


def test_existing_function_call_encoding_is_preserved():
    output_item = {
        "type": "response.output_item.done",
        "item": {
            "id": "fc_1",
            "type": "function_call",
            "status": "completed",
            "arguments": "{}",
            "call_id": "call_1",
            "name": "ping",
        },
        "output_index": 0,
    }
    body = (
        "event: response.output_item.done\n"
        f"data: {json.dumps(output_item)}\n\n"
    )

    result = runtime_entrypoint.extract_response_text_from_sse(body)

    assert result.startswith("__AIPROXY_TOOL_CALLS__")
