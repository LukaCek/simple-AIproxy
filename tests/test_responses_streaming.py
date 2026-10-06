import asyncio
import json
import sqlite3

import httpx
from fastapi.testclient import TestClient


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


class StaticChatSSEStream(httpx.AsyncByteStream):
    def __init__(self, text: str) -> None:
        self.text = text

    async def __aiter__(self):
        body = (
            'data: {"id":"ok","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"'
            + self.text
            + '"},"finish_reason":null}]}\n\n'
            'data: {"id":"ok","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        yield body.encode()


class SequencedStreamingRouteClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.hosts = []

    def build_request(self, method, url, json=None, headers=None):
        request = httpx.Request(method, url, json=json, headers=headers)
        request.extensions["json_payload"] = json
        return request

    async def send(self, request, stream=False):
        self.requests.append(request)
        self.hosts.append(request.url.host)
        if not self.responses:
            raise AssertionError("No fake response left for request")
        status_code, payload = self.responses.pop(0)
        if status_code == 200:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=StaticChatSSEStream(str(payload)),
                request=request,
            )
        return httpx.Response(status_code, json=payload, request=request)

    async def aclose(self):
        pass


def setup_streaming_key_db(streaming, tmp_path, monkeypatch):
    db_path = tmp_path / "streaming.db"
    monkeypatch.setattr(streaming._impl, "DB_PATH", db_path)
    streaming._impl.init_database()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)",
            ("test", "test-key", "now"),
        )
        conn.commit()


def test_streaming_reasoning_content_is_sanitized_and_retried(tmp_path, monkeypatch):
    streaming = get_streaming_module()
    setup_streaming_key_db(streaming, tmp_path, monkeypatch)
    fake = SequencedStreamingRouteClient(
        [
            (
                400,
                {
                    "error": {
                        "message": "'messages.2' : property 'reasoning_content' is unsupported",
                        "type": "invalid_request_error",
                    }
                },
            ),
            (200, "recovered"),
        ]
    )
    desired_config = {
        "providers": [
            {
                "name": "groq",
                "url": "http://groq.local/v1",
                "api_key": "k1",
                "models": ["openai/gpt-oss-120b"],
            }
        ],
        "groups": {
            "free-models": {
                "strategy": "fallback",
                "members": [
                    {"provider": "groq", "model": "openai/gpt-oss-120b"}
                ],
            }
        },
    }

    streaming._impl.config_data = desired_config
    with TestClient(streaming.app) as client:
        monkeypatch.setattr(streaming._impl, "http_client", fake)
        streaming._impl.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "free-models",
                "stream": True,
                "messages": [
                    {"role": "user", "content": "start"},
                    {
                        "role": "assistant",
                        "content": "previous",
                        "reasoning_content": "private reasoning",
                    },
                    {"role": "user", "content": "continue"},
                ],
            },
        )

    assert response.status_code == 200
    assert "recovered" in response.text
    assert fake.hosts == ["groq.local", "groq.local"]
    first_payload = fake.requests[0].extensions["json_payload"]
    second_payload = fake.requests[1].extensions["json_payload"]
    assert first_payload["messages"][1]["reasoning_content"] == "private reasoning"
    assert "reasoning_content" not in second_payload["messages"][1]


def test_streaming_413_falls_back_to_next_provider(tmp_path, monkeypatch):
    streaming = get_streaming_module()
    setup_streaming_key_db(streaming, tmp_path, monkeypatch)
    fake = SequencedStreamingRouteClient(
        [
            (
                413,
                {
                    "error": {
                        "message": "Request too large for model on tokens per minute",
                        "type": "tokens",
                        "code": "rate_limit_exceeded",
                    }
                },
            ),
            (200, "fallback worked"),
        ]
    )
    desired_config = {
        "providers": [
            {
                "name": "groq",
                "url": "http://groq.local/v1",
                "api_key": "k1",
                "models": ["openai/gpt-oss-120b"],
            },
            {
                "name": "mistral",
                "url": "http://mistral.local/v1",
                "api_key": "k2",
                "models": ["mistral-large-latest"],
            },
        ],
        "groups": {
            "free-models": {
                "strategy": "fallback",
                "members": [
                    {"provider": "groq", "model": "openai/gpt-oss-120b"},
                    {"provider": "mistral", "model": "mistral-large-latest"},
                ],
            }
        },
    }

    streaming._impl.config_data = desired_config
    with TestClient(streaming.app) as client:
        monkeypatch.setattr(streaming._impl, "http_client", fake)
        streaming._impl.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "free-models",
                "stream": True,
                "messages": [{"role": "user", "content": "large request"}],
            },
        )

    assert response.status_code == 200
    assert "fallback worked" in response.text
    assert fake.hosts == ["groq.local", "mistral.local"]
