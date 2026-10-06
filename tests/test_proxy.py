import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient

import main


class FakeChatClient:
    def __init__(self):
        self.hosts = []
        self.requests = []

    def build_request(self, method, url, json=None, headers=None):
        request = httpx.Request(method, url, json=json, headers=headers)
        request.extensions["json_payload"] = json
        return request

    async def send(self, request, stream=False):
        self.hosts.append(request.url.host)
        self.requests.append(request)
        payload = {
            "id": "ok",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": f"from {request.url.host}"}}],
        }
        return httpx.Response(200, json=payload, request=request)

    async def aclose(self):
        pass


class FakeResponsesClient(FakeChatClient):
    async def post(self, url, json=None, headers=None, timeout=None, data=None):
        self.hosts.append(httpx.URL(url).host)
        self.requests.append({"url": url, "json": json, "headers": headers, "data": data, "timeout": timeout})
        return httpx.Response(200, json={"output_text": "responses ok"}, request=httpx.Request("POST", url))




class FakeCloudflareResponsesClient(FakeChatClient):
    async def post(self, url, json=None, headers=None, timeout=None, data=None):
        self.hosts.append(httpx.URL(url).host)
        self.requests.append({"url": url, "json": json, "headers": headers, "data": data, "timeout": timeout})
        html = "<html><body><script>window._cf_chl_opt={};</script><span>Enable JavaScript and cookies to continue</span></body></html>"
        return httpx.Response(200, content=html.encode(), headers={"content-type": "text/html; charset=utf-8"}, request=httpx.Request("POST", url))



class FakeExpiredTokenResponsesClient(FakeChatClient):
    async def post(self, url, json=None, headers=None, timeout=None, data=None):
        self.hosts.append(httpx.URL(url).host)
        self.requests.append({"url": url, "json": json, "headers": headers, "data": data, "timeout": timeout})
        body = {"error": {"message": "Provided authentication token is expired. Please try signing in again.", "code": "token_expired"}, "status": 401}
        return httpx.Response(401, json=body, request=httpx.Request("POST", url))

class TimeoutChatClient(FakeChatClient):
    async def send(self, request, stream=False):
        self.hosts.append(request.url.host)
        self.requests.append(request)
        raise httpx.ReadTimeout("", request=request)


def setup_key_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.init_database()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)",
            ("test", "test-key", "now"),
        )
        conn.commit()


def test_resolve_endpoint_url_ollama_bare_host_uses_v1():
    assert main.resolve_endpoint_url({"url": "http://localhost:11434"}) == "http://localhost:11434/v1/chat/completions"
    assert main.resolve_endpoint_url({"url": "http://localhost:11434/v1"}) == "http://localhost:11434/v1/chat/completions"


def test_round_robin_rotates_first_provider():
    main.route_counters.clear()
    main.config_data = {"groups": {"gpt": {"strategy": "round_robin"}}}
    endpoints = [{"name": "a"}, {"name": "b"}]
    assert [e["name"] for e in main.route_endpoints("gpt", endpoints)] == ["a", "b"]
    assert [e["name"] for e in main.route_endpoints("gpt", endpoints)] == ["b", "a"]
    assert [e["name"] for e in main.route_endpoints("gpt", endpoints)] == ["a", "b"]


def test_chat_completions_uses_round_robin_between_providers(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeChatClient()
    monkeypatch.setattr(main, "http_client", fake)
    main.route_counters.clear()
    desired_config = {
        "providers": [
            {"name": "p1", "url": "http://p1.local/v1", "api_key": "k1", "models": ["m"]},
            {"name": "p2", "url": "http://p2.local/v1", "api_key": "k2", "models": ["m"]},
        ],
        "groups": {
            "gpt": {
                "strategy": "round_robin",
                "members": [
                    {"provider": "p1", "model": "m"},
                    {"provider": "p2", "model": "m"},
                ],
            }
        },
    }
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        for _ in range(4):
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={"model": "gpt", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert response.status_code == 200
    assert fake.hosts == ["p1.local", "p2.local", "p1.local", "p2.local"]


def test_direct_provider_model_is_routable_and_logged(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeChatClient()
    monkeypatch.setattr(main, "http_client", fake)
    main.route_counters.clear()
    desired_config = {
        "providers": [
            {"name": "groq", "url": "http://groq.local/v1", "api_key": "k1", "models": ["llama-3.1-8b-instant"]},
        ],
        "groups": {},
    }
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    assert fake.hosts == ["groq.local"]
    with sqlite3.connect(tmp_path / "app.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT api_key_name, requested_model, provider_name, provider_model, prompt, output, first_response_ms, total_ms FROM Logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["api_key_name"] == "test"
    assert row["requested_model"] == "llama-3.1-8b-instant"
    assert row["provider_name"] == "groq"
    assert row["provider_model"] == "llama-3.1-8b-instant"
    assert "hi" in row["prompt"]
    assert "from groq.local" in row["output"]
    assert row["first_response_ms"] is not None
    assert row["total_ms"] is not None


def test_ollama_homeassistant_payload_gets_safe_token_budget_and_keeps_thinking(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeChatClient()
    monkeypatch.setattr(main, "http_client", fake)
    desired_config = {
        "providers": [
            {"name": "ollamaVOBLAK", "url": "http://ollama.local/v1", "api_key": "", "models": ["gemma4:26b"]},
        ],
        "groups": {},
    }
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "gemma4:26b", "max_tokens": 150, "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    sent_payload = fake.requests[0].extensions["json_payload"]
    assert sent_payload["model"] == "gemma4:26b"
    assert sent_payload["max_tokens"] == main.MIN_COMPLETION_TOKENS
    assert "think" not in sent_payload


def test_responses_adapter_returns_chat_completion_and_sse(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeResponsesClient()
    monkeypatch.setattr(main, "http_client", fake)
    main.route_counters.clear()
    desired_config = {
        "providers": [
            {
                "name": "codex-a",
                "url": "https://chatgpt.com/backend-api/codex",
                "api_key": "token",
                "models": ["gpt-5.5"],
                "api_mode": "codex_responses",
            }
        ],
        "groups": {"gpt-5.5": {"members": [{"provider": "codex-a", "model": "gpt-5.5"}]}},
    }
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "responses ok"

        stream_response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "gpt-5.5", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert stream_response.status_code == 200
        assert "data: [DONE]" in stream_response.text

    assert fake.requests[0]["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert fake.requests[0]["json"]["input"] == [{"role": "user", "content": "hi"}]
    assert fake.requests[0]["json"]["instructions"] == "You are a helpful assistant."
    assert fake.requests[0]["json"]["store"] is False
    assert fake.requests[0]["json"]["stream"] is True
    assert fake.requests[0]["timeout"] == main.UPSTREAM_REQUEST_TIMEOUT_SECONDS


def test_responses_adapter_converts_chat_vision_parts(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeResponsesClient()
    monkeypatch.setattr(main, "http_client", fake)
    desired_config = {
        "providers": [
            {
                "name": "codex-vision",
                "url": "https://chatgpt.com/backend-api/codex",
                "api_key": "token",
                "models": ["gpt-5.5"],
                "api_mode": "codex_responses",
            }
        ],
        "groups": {
            "vision": {
                "members": [
                    {"provider": "codex-vision", "model": "gpt-5.5"}
                ]
            }
        },
    }
    image_data_url = "data:image/png;base64,iVBORw0KGgo="

    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "vision",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "What is in this image?"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image_data_url,
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert fake.requests[0]["json"]["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "What is in this image?"},
                {
                    "type": "input_image",
                    "image_url": image_data_url,
                    "detail": "high",
                },
            ],
        }
    ]


def test_responses_vision_conversion_accepts_string_url_and_is_idempotent():
    converted = main.chat_to_responses_payload(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": "https://example.com/a.png"},
                        {"type": "input_text", "text": "Already converted"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/b.png",
                        },
                    ],
                }
            ]
        },
        "gpt-5.5",
    )

    assert converted["input"][0]["content"] == [
        {"type": "input_image", "image_url": "https://example.com/a.png"},
        {"type": "input_text", "text": "Already converted"},
        {"type": "input_image", "image_url": "https://example.com/b.png"},
    ]


def test_codex_responses_html_challenge_is_not_returned_as_success(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeCloudflareResponsesClient()
    monkeypatch.setattr(main, "http_client", fake)
    main.route_counters.clear()
    desired_config = {
        "providers": [
            {
                "name": "codex-a",
                "url": "https://chatgpt.com/backend-api/codex",
                "api_key": "token",
                "models": ["gpt-5.5"],
                "api_mode": "codex_responses",
            }
        ],
        "groups": {},
    }
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 502
    assert "Cloudflare" in response.json()["detail"]
    assert "<html" not in response.text.lower()
    with sqlite3.connect(tmp_path / "app.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status_code, error FROM Logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status_code"] == 502
    assert "Cloudflare" in row["error"]
    assert "<html" not in row["error"].lower()


def test_codex_responses_token_expired_marks_provider_for_reauth(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.yml")
    fake = FakeExpiredTokenResponsesClient()
    monkeypatch.setattr(main, "http_client", fake)
    main.route_counters.clear()
    desired_config = {
        "providers": [
            {
                "name": "codex-a",
                "url": "https://chatgpt.com/backend-api/codex",
                "api_key": "expired-token",
                "access_token": "expired-token",
                "refresh_token": "refresh-token",
                "models": ["gpt-5.5"],
                "api_mode": "codex_responses",
                "oauth": True,
            }
        ],
        "groups": {},
    }
    main.config_data = desired_config
    main.save_config(desired_config)
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 502
    assert "Reauthenticate" in response.json()["detail"]
    assert "token_expired" not in response.text
    provider = main.find_provider("codex-a")
    assert provider["oauth_reauth_required"] is True
    assert main.oauth_provider_connected(provider) is False
    with sqlite3.connect(tmp_path / "app.db") as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status_code, error FROM Logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status_code"] == 502
    assert "Reauthenticate" in row["error"]


def test_provider_timeout_logs_exception_class(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = TimeoutChatClient()
    monkeypatch.setattr(main, "http_client", fake)
    main.config_data = {
        "providers": [{"name": "ollamaVOBLAK", "url": "http://ollama.local/v1", "api_key": "", "models": ["gemma4:26b"]}],
        "groups": {},
    }

    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = {
            "providers": [{"name": "ollamaVOBLAK", "url": "http://ollama.local/v1", "api_key": "", "models": ["gemma4:26b"]}],
            "groups": {},
        }
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "gemma4:26b", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 502
    with sqlite3.connect(tmp_path / "app.db") as conn:
        row = conn.execute("SELECT error, status_code FROM Logs ORDER BY id DESC LIMIT 1").fetchone()
    assert row[1] == 502
    assert row[0] == "ollamaVOBLAK failed: ReadTimeout"


def test_extract_response_text_from_responses_sse_prefers_delta_once():
    body = b'''event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hel","text":"hel"}\n\nevent: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"lo","text":"lo"}\n\nevent: response.completed\ndata: {"type":"response.completed","output_text":"hello"}\n\ndata: [DONE]\n\n'''
    assert main.extract_response_text_from_sse(body) == "hello"


def test_extract_response_text_from_chat_sse():
    body = b'''data: {"choices":[{"delta":{"content":"hel"}}]}\n\ndata: {"choices":[{"delta":{"content":"lo"}}]}\n\ndata: [DONE]\n\n'''
    assert main.extract_response_text_from_sse(body) == "hello"


def test_responses_adapter_accepts_top_level_and_system_instructions():
    converted = main.chat_to_responses_payload(
        {
            "model": "gpt-5.5",
            "instructions": "Top level instruction.",
            "messages": [
                {"role": "system", "content": "System instruction."},
                {"role": "user", "content": "hi"},
            ],
            "store": False,
            "max_tokens": 10,
        },
        "gpt-5.5",
    )
    assert converted["instructions"] == "Top level instruction.\nSystem instruction."
    assert converted["input"] == [{"role": "user", "content": "hi"}]
    assert converted["store"] is False
    assert "max_output_tokens" not in converted


def test_nested_group_fallback_can_reference_round_robin_group():
    main.route_counters.clear()
    main.config_data = {
        "providers": [
            {"name": "codex-a", "url": "http://codex-a.local/v1", "models": ["gpt-5.5"]},
            {"name": "codex-b", "url": "http://codex-b.local/v1", "models": ["gpt-5.5"]},
            {"name": "groq", "url": "http://groq.local/v1", "models": ["llama-3.1-8b-instant"]},
        ],
        "groups": {
            "codex-pool": {
                "strategy": "round_robin",
                "members": [
                    {"provider": "codex-a", "model": "gpt-5.5"},
                    {"provider": "codex-b", "model": "gpt-5.5"},
                ],
            },
            "groq-fast": {
                "strategy": "fallback",
                "members": [{"provider": "groq", "model": "llama-3.1-8b-instant"}],
            },
            "gpt-5.5": {
                "strategy": "fallback",
                "members": [{"group": "codex-pool"}, {"group": "groq-fast"}],
            },
        },
    }

    first = main.resolve_requested_model("gpt-5.5")
    second = main.resolve_requested_model("gpt-5.5")

    assert [(endpoint["name"], endpoint["model"]) for endpoint in first] == [
        ("codex-a", "gpt-5.5"),
        ("codex-b", "gpt-5.5"),
        ("groq", "llama-3.1-8b-instant"),
    ]
    assert [(endpoint["name"], endpoint["model"]) for endpoint in second] == [
        ("codex-b", "gpt-5.5"),
        ("codex-a", "gpt-5.5"),
        ("groq", "llama-3.1-8b-instant"),
    ]


def test_group_cycle_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.yml")
    main.config_data = {
        "providers": [{"name": "p", "url": "http://p.local/v1", "models": ["m"]}],
        "groups": {"a": {"members": [{"group": "b"}]}, "b": {"members": [{"provider": "p", "model": "m"}]}},
    }

    try:
        main.save_group("b", "", "fallback", [{"group": "a"}], original_name="b")
    except main.HTTPException as exc:
        assert exc.status_code == 400
        assert "cannot contain itself" in exc.detail
    else:
        raise AssertionError("Expected cycle rejection")


class SequencedChatClient(FakeChatClient):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    async def send(self, request, stream=False):
        self.hosts.append(request.url.host)
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No fake response left for request")
        status_code, payload = self.responses.pop(0)
        return httpx.Response(status_code, json=payload, request=request)


def test_reasoning_content_unsupported_is_sanitized_and_retried(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = SequencedChatClient(
        [
            (
                400,
                {
                    "error": {
                        "message": "'messages.2' : for 'role:assistant' the following must be satisfied[('messages.2' : property 'reasoning_content' is unsupported)]",
                        "type": "invalid_request_error",
                    }
                },
            ),
            (
                200,
                {
                    "id": "ok",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "recovered",
                            }
                        }
                    ],
                },
            ),
        ]
    )
    monkeypatch.setattr(main, "http_client", fake)
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
                    {
                        "provider": "groq",
                        "model": "openai/gpt-oss-120b",
                    }
                ],
            }
        },
    }

    payload = {
        "model": "free-models",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "do something"},
            {
                "role": "assistant",
                "content": "previous answer",
                "reasoning_content": "internal reasoning",
            },
            {"role": "user", "content": "continue"},
        ],
    }

    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json=payload,
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "recovered"
    assert fake.hosts == ["groq.local", "groq.local"]
    first_payload = fake.requests[0].extensions["json_payload"]
    second_payload = fake.requests[1].extensions["json_payload"]
    assert first_payload["messages"][2]["reasoning_content"] == "internal reasoning"
    assert "reasoning_content" not in second_payload["messages"][2]
    assert payload["messages"][2]["reasoning_content"] == "internal reasoning"


def test_413_falls_back_to_next_provider(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = SequencedChatClient(
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
            (
                200,
                {
                    "id": "ok",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "from fallback",
                            }
                        }
                    ],
                },
            ),
        ]
    )
    monkeypatch.setattr(main, "http_client", fake)
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

    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "free-models",
                "messages": [{"role": "user", "content": "large request"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from fallback"
    assert fake.hosts == ["groq.local", "mistral.local"]


def test_plain_400_is_returned_without_trying_next_provider(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = SequencedChatClient(
        [
            (
                400,
                {
                    "error": {
                        "message": "Invalid tool schema",
                        "type": "invalid_request_error",
                    }
                },
            ),
            (
                200,
                {
                    "id": "unexpected",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "should not be reached",
                            }
                        }
                    ],
                },
            ),
        ]
    )
    monkeypatch.setattr(main, "http_client", fake)
    desired_config = {
        "providers": [
            {
                "name": "provider-a",
                "url": "http://provider-a.local/v1",
                "api_key": "k1",
                "models": ["m1"],
            },
            {
                "name": "provider-b",
                "url": "http://provider-b.local/v1",
                "api_key": "k2",
                "models": ["m2"],
            },
        ],
        "groups": {
            "g": {
                "strategy": "fallback",
                "members": [
                    {"provider": "provider-a", "model": "m1"},
                    {"provider": "provider-b", "model": "m2"},
                ],
            }
        },
    }

    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "g", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid tool schema"
    assert fake.hosts == ["provider-a.local"]


def test_429_still_falls_back_to_next_provider(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = SequencedChatClient(
        [
            (
                429,
                {
                    "error": {
                        "message": "rate limited",
                        "type": "rate_limit_error",
                    }
                },
            ),
            (
                200,
                {
                    "id": "ok",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "second provider",
                            }
                        }
                    ],
                },
            ),
        ]
    )
    monkeypatch.setattr(main, "http_client", fake)
    desired_config = {
        "providers": [
            {
                "name": "provider-a",
                "url": "http://provider-a.local/v1",
                "api_key": "k1",
                "models": ["m1"],
            },
            {
                "name": "provider-b",
                "url": "http://provider-b.local/v1",
                "api_key": "k2",
                "models": ["m2"],
            },
        ],
        "groups": {
            "g": {
                "strategy": "fallback",
                "members": [
                    {"provider": "provider-a", "model": "m1"},
                    {"provider": "provider-b", "model": "m2"},
                ],
            }
        },
    }

    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "g", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "second provider"
    assert fake.hosts == ["provider-a.local", "provider-b.local"]


def test_preflight_skips_obviously_oversized_tpm_provider(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeChatClient()
    monkeypatch.setattr(main, "http_client", fake)
    desired_config = {
        "providers": [
            {
                "name": "limited",
                "url": "http://limited.local/v1",
                "api_key": "k1",
                "models": ["m1"],
                "model_metadata": {
                    "m1": {"context_tokens": 100000, "free_limits": {"tpm": 100}}
                },
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
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "g",
                "messages": [{"role": "user", "content": "x" * 1200}],
            },
        )
    assert response.status_code == 200
    assert fake.hosts == ["fallback.local"]


def test_preflight_allows_small_request_to_limited_provider(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeChatClient()
    monkeypatch.setattr(main, "http_client", fake)
    desired_config = {
        "providers": [
            {
                "name": "limited",
                "url": "http://limited.local/v1",
                "api_key": "k1",
                "models": ["m1"],
                "model_metadata": {
                    "m1": {"context_tokens": 100000, "free_limits": {"tpm": 8000}}
                },
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
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "g",
                "messages": [{"role": "user", "content": "short"}],
            },
        )
    assert response.status_code == 200
    assert fake.hosts == ["limited.local"]


def test_preflight_skips_obviously_oversized_context_provider(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeChatClient()
    monkeypatch.setattr(main, "http_client", fake)
    desired_config = {
        "providers": [
            {
                "name": "tiny-context",
                "url": "http://tiny.local/v1",
                "api_key": "k1",
                "models": ["m1"],
                "model_metadata": {"m1": {"context_tokens": 100}},
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
                    {"provider": "tiny-context", "model": "m1"},
                    {"provider": "fallback", "model": "m2"},
                ],
            }
        },
    }
    main.config_data = desired_config
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        main.config_data = desired_config
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "g",
                "messages": [{"role": "user", "content": "x" * 1200}],
            },
        )
    assert response.status_code == 200
    assert fake.hosts == ["fallback.local"]
