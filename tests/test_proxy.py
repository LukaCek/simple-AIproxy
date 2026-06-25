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
