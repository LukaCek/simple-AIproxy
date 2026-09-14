import asyncio
import json

import httpx


def get_streaming_module():
    # Import lazily so the production Codex runtime extension does not mutate
    # global model configuration while pytest is still collecting older tests.
    import responses_streaming_entrypoint as streaming

    return streaming


class GateSSEStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.completed = False

    async def __aiter__(self):
        yield (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"hello "}\n\n'
        )
        await self.release.wait()
        yield (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"world"}\n\n'
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        )
        self.completed = True


class SilentSSEStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def __aiter__(self):
        await self.release.wait()
        yield b"event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"


class ToolCallSSEStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        events = [
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "call_id": "call_1",
                        "type": "function_call",
                        "name": "ping",
                        "arguments": "",
                    },
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "output_index": 0,
                    "delta": '{"value":',
                },
            ),
            (
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "output_index": 0,
                    "delta": "1}",
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc_1",
                        "call_id": "call_1",
                        "type": "function_call",
                        "name": "ping",
                        "arguments": '{"value":1}',
                    },
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"status": "completed"},
                },
            ),
        ]
        body = "".join(
            f"event: {name}\ndata: {json.dumps(event)}\n\n"
            for name, event in events
        )
        yield body.encode()


def make_response(stream: httpx.AsyncByteStream) -> httpx.Response:
    request = httpx.Request("POST", "https://chatgpt.com/backend-api/codex/responses")
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=stream,
        request=request,
    )


def data_payload(chunk: bytes):
    text = chunk.decode()
    if not text.startswith("data: "):
        return None
    raw = text[6:].strip()
    if raw == "[DONE]":
        return raw
    return json.loads(raw)


def test_first_chunk_is_emitted_before_upstream_completion():
    streaming = get_streaming_module()

    async def scenario():
        upstream = GateSSEStream()
        response = make_response(upstream)
        seen_text = []
        first_calls = []
        iterator = streaming.responses_to_chat_sse(
            response,
            "gpt-5.6-sol",
            heartbeat_seconds=0.05,
            on_first=lambda: first_calls.append(True),
            on_text=seen_text.append,
        )

        role = await anext(iterator)
        role_payload = data_payload(role)
        assert role_payload["choices"][0]["delta"]["role"] == "assistant"
        assert first_calls == [True]
        assert upstream.completed is False

        first_text = await anext(iterator)
        first_payload = data_payload(first_text)
        assert first_payload["choices"][0]["delta"]["content"] == "hello "
        assert seen_text == ["hello "]
        assert upstream.completed is False

        upstream.release.set()
        remainder = [chunk async for chunk in iterator]
        payloads = [data_payload(chunk) for chunk in remainder]
        assert any(
            isinstance(payload, dict)
            and payload["choices"][0]["delta"].get("content") == "world"
            for payload in payloads
        )
        assert payloads[-1] == "[DONE]"
        assert upstream.completed is True
        await response.aclose()

    asyncio.run(scenario())


def test_silent_reasoning_emits_heartbeat_without_cancelling_read():
    streaming = get_streaming_module()

    async def scenario():
        upstream = SilentSSEStream()
        response = make_response(upstream)
        iterator = streaming.responses_to_chat_sse(
            response,
            "gpt-5.6-sol",
            heartbeat_seconds=0.01,
        )

        await anext(iterator)  # immediate assistant role
        heartbeat = await anext(iterator)
        assert heartbeat == b": aiproxy keep-alive\n\n"

        # A timeout heartbeat must not cancel the pending upstream read.
        upstream.release.set()
        remainder = [chunk async for chunk in iterator]
        assert remainder[-1] == b"data: [DONE]\n\n"
        await response.aclose()

    asyncio.run(scenario())


def test_function_calls_are_streamed_as_openai_tool_call_deltas():
    streaming = get_streaming_module()

    async def scenario():
        response = make_response(ToolCallSSEStream())
        chunks = [
            chunk
            async for chunk in streaming.responses_to_chat_sse(
                response,
                "gpt-5.6-sol",
                heartbeat_seconds=0.05,
            )
        ]
        payloads = [
            payload for payload in map(data_payload, chunks) if isinstance(payload, dict)
        ]

        tool_deltas = [
            payload["choices"][0]["delta"]["tool_calls"][0]
            for payload in payloads
            if payload["choices"][0]["delta"].get("tool_calls")
        ]
        assert tool_deltas[0]["id"] == "call_1"
        assert tool_deltas[0]["function"]["name"] == "ping"
        assert "".join(
            delta.get("function", {}).get("arguments", "")
            for delta in tool_deltas
        ) == '{"value":1}'
        assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"
        assert chunks[-1] == b"data: [DONE]\n\n"
        await response.aclose()

    asyncio.run(scenario())


def test_production_route_is_replaced_once():
    streaming = get_streaming_module()
    routes = [
        route
        for route in streaming.app.router.routes
        if getattr(route, "path", None) == "/v1/chat/completions"
        and "POST" in (getattr(route, "methods", None) or set())
    ]
    assert len(routes) == 1
    assert routes[0].endpoint is streaming.chat_completions
