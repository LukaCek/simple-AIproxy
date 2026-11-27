import asyncio
import json
import sqlite3

import httpx
import pytest
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
    streaming._impl.provider_cooldowns.clear()
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
    with sqlite3.connect(tmp_path / "streaming.db") as conn:
        conn.row_factory = sqlite3.Row
        attempts = conn.execute(
            "SELECT request_id, attempt_no, provider_name, status_code, action "
            "FROM ProviderAttempts ORDER BY attempt_no"
        ).fetchall()
        final_log = conn.execute(
            "SELECT request_id FROM Logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert [(row["attempt_no"], row["provider_name"], row["status_code"], row["action"]) for row in attempts] == [
        (1, "groq", 413, "fallback"),
        (2, "mistral", 200, "success"),
    ]
    assert attempts[0]["request_id"] == attempts[1]["request_id"]
    assert final_log["request_id"] == attempts[0]["request_id"]


def test_streaming_429_cooldown_skips_provider_on_next_request(tmp_path, monkeypatch):
    streaming = get_streaming_module()
    setup_streaming_key_db(streaming, tmp_path, monkeypatch)
    fake = SequencedStreamingRouteClient(
        [
            (429, {"error": {"message": "rate limited"}}),
            (200, "fallback one"),
            (200, "fallback two"),
        ]
    )
    desired_config = {
        "providers": [
            {
                "name": "limited",
                "url": "http://limited.local/v1",
                "api_key": "k1",
                "models": ["m1"],
            },
            {
                "name": "fallback",
                "url": "http://fallback.local/v1",
                "api_key": "k2",
                "models": ["m2"],
            },
        ],
        "groups": {
            "g": {
                "strategy": "fallback",
                "members": [
                    {"provider": "limited", "model": "m1"},
                    {"provider": "fallback", "model": "m2"},
                ],
            }
        },
    }

    streaming._impl.config_data = desired_config
    with TestClient(streaming.app) as client:
        monkeypatch.setattr(streaming._impl, "http_client", fake)
        streaming._impl.config_data = desired_config
        first = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "g",
                "stream": True,
                "messages": [{"role": "user", "content": "one"}],
            },
        )
        second = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "g",
                "stream": True,
                "messages": [{"role": "user", "content": "two"}],
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert fake.hosts == ["limited.local", "fallback.local", "fallback.local"]
    with sqlite3.connect(tmp_path / "streaming.db") as conn:
        actions = [
            row[0]
            for row in conn.execute(
                "SELECT action FROM ProviderAttempts ORDER BY id"
            ).fetchall()
        ]
    assert actions == ["fallback", "success", "cooldown_skip", "success"]


class FreePoolBadRequestClient(SequencedStreamingRouteClient):
    async def send(self, request, stream=False):
        if request.extensions["json_payload"].get("stream"):
            return await super().send(request, stream=stream)
        self.requests.append(request)
        self.hosts.append(request.url.host)
        status_code, payload = self.responses.pop(0)
        if status_code == 200:
            payload = {"choices": [{"message": {"role": "assistant", "content": payload}}]}
        return httpx.Response(status_code, json=payload, request=request)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("upstream_status", [400, 404, 410, 422])
@pytest.mark.parametrize("scenario", ["recover", "tool_history", "exhaust", "mixed_exhaust", "direct", "other_group", "malformed", "malformed_role"])
def test_free_pool_upstream_client_error_fallback(tmp_path, monkeypatch, stream, upstream_status, scenario):
    streaming = get_streaming_module()
    setup_streaming_key_db(streaming, tmp_path, monkeypatch)
    first_error = {"error": {"code": upstream_status, "message": "provider-specific bad request"}}
    second_error = {"error": {"message": "second provider rejected"}}
    outcomes = [(upstream_status, first_error)]
    if scenario in {"recover", "tool_history"}:
        outcomes.append((200, "recovered"))
    elif scenario == "exhaust":
        outcomes.append((upstream_status, second_error))
    elif scenario == "mixed_exhaust":
        outcomes.append((429, second_error))
    fake = FreePoolBadRequestClient(outcomes)
    members = [{"provider": "gemini", "model": "gemini-test"}, {"provider": "groq", "model": "groq-test"}]
    desired_config = {
        "providers": [
            {"name": "gemini", "url": "http://gemini.local/v1", "api_key": "k1", "models": ["gemini-test"]},
            {"name": "groq", "url": "http://groq.local/v1", "api_key": "k2", "models": ["groq-test"]},
        ],
        "groups": {
            "free-models": {"strategy": "fallback", "members": members},
            "other": {"strategy": "fallback", "members": members},
        },
    }
    model = {"direct": "gemini-test", "other_group": "other"}.get(scenario, "free-models")
    messages = [{"role": "user", "content": "start"}]
    if scenario == "malformed":
        messages = [{"content": "missing role"}]
    if scenario == "malformed_role":
        messages = [{"role": {}, "content": "bad role"}]
    payload = {"model": model, "messages": messages, "stream": stream}
    if scenario == "tool_history":
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "pong"},
        ])
        payload["tools"] = [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}]
    streaming._impl.config_data = desired_config
    with TestClient(streaming.app) as client:
        monkeypatch.setattr(streaming._impl, "http_client", fake)
        streaming._impl.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json=payload,
        )

    retry = scenario in {"recover", "tool_history", "exhaust", "mixed_exhaust"}
    assert fake.hosts == (["gemini.local", "groq.local"] if retry else ["gemini.local"])
    for request in fake.requests:
        assert request.extensions["json_payload"]["messages"] == messages
        assert request.extensions["json_payload"].get("tools") == payload.get("tools")
    if scenario in {"recover", "tool_history"}:
        assert response.status_code == 200
        assert "recovered" in response.text
    else:
        assert response.status_code == upstream_status
        assert response.json() == first_error
    with sqlite3.connect(tmp_path / "streaming.db") as conn:
        attempts = conn.execute(
            "SELECT request_id, provider_name, status_code, action FROM ProviderAttempts ORDER BY attempt_no"
        ).fetchall()
        final = conn.execute(
            "SELECT request_id, provider_name, status_code FROM Logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert len(attempts) == (2 if retry else 1)
    assert attempts[0][3] == ("fallback" if retry else "returned_error")
    assert all(row[0] == final[0] for row in attempts)
    assert final[1:] == (("groq", 200) if scenario in {"recover", "tool_history"} else ("gemini", upstream_status))
    if retry:
        assert attempts[1][3] == ("success" if scenario in {"recover", "tool_history"} else "fallback")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("retry_succeeds", [True, False])
def test_mistral_422_reasoning_content_retry_then_fallback(tmp_path, monkeypatch, stream, retry_succeeds):
    streaming = get_streaming_module()
    setup_streaming_key_db(streaming, tmp_path, monkeypatch)
    error = {"detail": [{"type": "extra_forbidden",
                         "loc": ["body", "messages", 2, "assistant", "reasoning_content"],
                         "msg": "Extra inputs are not permitted", "input": "private reasoning"}]}
    outcomes = [(422, error)]
    if retry_succeeds:
        outcomes.append((200, "recovered"))
    else:
        outcomes.extend([(422, {"detail": [{"type": "value_error", "loc": ["body", "tools"]}]}),
                         (200, "recovered")])
    fake = FreePoolBadRequestClient(outcomes)
    config = {
        "providers": [
            {"name": "mistral", "url": "http://mistral.local/v1", "api_key": "k1", "models": ["m1"]},
            {"name": "backup", "url": "http://backup.local/v1", "api_key": "k2", "models": ["m2"]},
        ],
        "groups": {"free-models": {"strategy": "fallback", "members": [
            {"provider": "mistral", "model": "m1"}, {"provider": "backup", "model": "m2"}
        ]}},
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": None, "reasoning_content": "private reasoning",
         "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "pong"},
    ]
    tools = [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object"}}}]
    streaming._impl.config_data = config
    with TestClient(streaming.app) as client:
        monkeypatch.setattr(streaming._impl, "http_client", fake)
        streaming._impl.config_data = config
        response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer test-key"},
                               json={"model": "free-models", "stream": stream, "messages": messages, "tools": tools})
    assert response.status_code == 200
    assert "recovered" in response.text
    assert fake.hosts == (["mistral.local"] * 2 + ([] if retry_succeeds else ["backup.local"]))
    original, sanitized = [r.extensions["json_payload"] for r in fake.requests[:2]]
    assert original["messages"] == messages
    expected = [dict(m) for m in messages]
    expected[2].pop("reasoning_content")
    assert sanitized["messages"] == expected
    assert sanitized["tools"] == tools
    if not retry_succeeds:
        assert fake.requests[2].extensions["json_payload"]["messages"] == messages
    with sqlite3.connect(tmp_path / "streaming.db") as conn:
        attempts = conn.execute("SELECT status_code, action FROM ProviderAttempts ORDER BY attempt_no").fetchall()
    assert attempts == ([(422, "retry_sanitized"), (200, "success")] if retry_succeeds else
                        [(422, "retry_sanitized"), (422, "fallback"), (200, "success")])


def test_422_reasoning_detection_uses_validation_location_not_echoed_input():
    streaming = get_streaming_module()
    detect = streaming._impl.is_unsupported_reasoning_content_error
    error = {"detail": [{"type": "extra_forbidden", "loc": ["body", "messages", 2, "assistant", "reasoning_content"]}]}
    assert detect(422, json.dumps(error))
    assert detect(422, json.dumps([error]))
    assert not detect(422, json.dumps({"detail": [{"type": "extra_forbidden", "loc": ["body", "tools"], "input": "reasoning_content unsupported"}]}))
    assert not detect(422, "reasoning_content unsupported")
    assert not detect(429, json.dumps(error))
    assert detect(400, "reasoning_content is unsupported")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status_code,error_body,quarantine", [
    (410, {"error": {"message": "Gone"}}, True),
    (404, {"error": {"code": "model_not_found", "message": "model missing"}}, True),
    (404, {"error": {"message": "resource not found"}}, False),
])
def test_free_pool_unavailable_model_cooldown_and_recheck(tmp_path, monkeypatch, stream, status_code, error_body, quarantine):
    streaming = get_streaming_module()
    impl = streaming._impl
    setup_streaming_key_db(streaming, tmp_path, monkeypatch)
    monkeypatch.setattr(impl, "FREE_POOL_UNAVAILABLE_COOLDOWN_SECONDS", 300)
    # Second request must skip a quarantined model, but an ambiguous 404
    # must be attempted again. Direct requests bypass the free-pool quarantine.
    outcomes = [(status_code, error_body), (200, "backup one")]
    if not quarantine:
        outcomes.append((status_code, error_body))
    outcomes.extend([(200, "backup two"), (200, "direct"), (200, "recovered")])
    fake = FreePoolBadRequestClient(outcomes)
    config = {
        "providers": [
            {"name": "gone", "url": "http://gone.local/v1", "api_key": "k1", "models": ["m1"]},
            {"name": "backup", "url": "http://backup.local/v1", "api_key": "k2", "models": ["m2"]},
        ],
        "groups": {"free-models": {"strategy": "fallback", "members": [
            {"provider": "gone", "model": "m1"}, {"provider": "backup", "model": "m2"}
        ]}},
    }
    endpoint = {"name": "gone", "model": "m1"}
    impl.config_data = config
    with TestClient(streaming.app) as client:
        monkeypatch.setattr(impl, "http_client", fake)
        impl.config_data = config

        def post(model):
            return client.post("/v1/chat/completions", headers={"Authorization": "Bearer test-key"},
                               json={"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]})

        assert "backup one" in post("free-models").text
        assert (impl.provider_cooldown_remaining(endpoint, scope=impl.FREE_POOL_UNAVAILABLE_SCOPE) > 0) == quarantine
        assert impl.provider_cooldown_remaining(endpoint) == 0
        assert impl.provider_cooldown_remaining({"name": "gone", "model": "other"}, scope=impl.FREE_POOL_UNAVAILABLE_SCOPE) == 0
        assert "backup two" in post("free-models").text
        assert "direct" in post("m1").text
        # Expire only this scoped entry; no sleep or external provider probes.
        with impl.provider_cooldowns_lock:
            impl.provider_cooldowns[f"{impl.FREE_POOL_UNAVAILABLE_SCOPE}::{impl.provider_cooldown_key(endpoint)}"] = 0
        assert "recovered" in post("free-models").text
        assert impl.provider_cooldown_remaining(endpoint, scope=impl.FREE_POOL_UNAVAILABLE_SCOPE) == 0

    expected = ["gone.local", "backup.local"]
    if not quarantine:
        expected.append("gone.local")
    expected.extend(["backup.local", "gone.local", "gone.local"])
    assert fake.hosts == expected
    assert not fake.responses
    with sqlite3.connect(tmp_path / "streaming.db") as conn:
        actions = [row[0] for row in conn.execute("SELECT action FROM ProviderAttempts ORDER BY id")]
    assert actions == ["fallback", "success", "cooldown_skip" if quarantine else "fallback", "success", "success", "success"]
