import asyncio
import sqlite3
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


def test_add_codex_profile_creates_provider_and_group(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    main.config_data = {"providers": [], "groups": {}}

    main.add_codex_profile(
        name="codex-a",
        access_token="access-123",
        refresh_token="refresh-123",
        description="First profile",
    )

    provider = main.find_provider("codex-a")
    assert provider is not None
    assert provider["url"] == "https://chatgpt.com/backend-api/codex"
    assert provider["api_mode"] == "codex_responses"
    assert provider["api_key"] == "access-123"
    assert provider["access_token"] == "access-123"
    assert provider["refresh_token"] == "refresh-123"
    assert provider["models"] == ["gpt-5.5"]

    assert main.config_data["groups"]["gpt-5.5"]["strategy"] == "round_robin"
    assert main.config_data["groups"]["gpt-5.5"]["members"] == [
        {"provider": "codex-a", "model": "gpt-5.5"}
    ]
    assert config_path.exists()


def test_add_codex_profile_does_not_duplicate_group_member(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.yml")
    main.config_data = {"providers": [], "groups": {}}

    main.add_codex_profile(name="codex-a")
    main.ensure_group_member("gpt-5.5", "codex-a", "gpt-5.5")

    assert main.config_data["groups"]["gpt-5.5"]["members"] == [
        {"provider": "codex-a", "model": "gpt-5.5"}
    ]


def test_admin_keys_page_renders_with_current_starlette_template_api(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text("providers: []\ngroups: {}\n", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = {"providers": [], "groups": {}}

    with TestClient(main.app) as client:
        response = client.get("/admin/keys", auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD))

    assert response.status_code == 200
    assert "API Keys" in response.text


def test_admin_providers_page_renders_codex_import_form(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text("providers: []\ngroups: {}\n", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = {"providers": [], "groups": {}}

    with TestClient(main.app) as client:
        response = client.get("/admin/providers", auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD))

    assert response.status_code == 200
    assert "Codex OAuth pool" in response.text
    assert "/admin/providers/codex-token" in response.text
    assert "data-test-provider=\"ollama-provider-form\"" in response.text
    assert "data-test-provider=\"openai-provider-form\"" in response.text
    assert "/admin/providers/test" in response.text


async def fake_provider_test(**kwargs):
    return {
        "success": True,
        "provider": kwargs["name"],
        "model": "llama-3.1-8b-instant",
        "status_code": 200,
        "response": "OK",
    }


def test_admin_provider_test_endpoint_tests_without_saving(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text("providers: []\ngroups: {}\n", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(main, "test_provider_candidate", fake_provider_test)
    main.config_data = main.load_config()

    with TestClient(main.app) as client:
        response = client.post(
            "/admin/providers/test",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={
                "name": "groq-test",
                "base_url": "https://api.groq.com/openai/v1",
                "api_key": "test-key",
                "models": "llama-3.1-8b-instant",
            },
        )

    assert response.status_code == 200
    assert response.json()["response"] == "OK"
    assert main.config_data["providers"] == []


class FakeProviderTestStream:
    def __init__(self, client, url, json=None, headers=None, timeout=None):
        self.client = client
        self.url = url
        self.json = json
        self.headers = headers
        self.timeout = timeout

    async def __aenter__(self):
        self.client.calls.append({"url": self.url, "json": self.json, "headers": self.headers, "timeout": self.timeout})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}, request=httpx.Request("POST", self.url))

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeProviderTestClient:
    def __init__(self):
        self.calls = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        assert method == "POST"
        return FakeProviderTestStream(self, url, json=json, headers=headers, timeout=timeout)

    async def aclose(self):
        pass


def test_provider_candidate_uses_one_hour_timeout_for_slow_ollama_load(monkeypatch):
    fake = FakeProviderTestClient()
    monkeypatch.setattr(main, "http_client", fake)

    result = asyncio.run(
        main.test_provider_candidate(
            name="ollama-local",
            base_url="http://ollama.local:11434/v1",
            api_key="",
            models_text="llama3.2",
        )
    )

    assert result["success"] is True
    assert fake.calls[0]["timeout"] == 3600.0


def test_admin_codex_token_form_adds_profile(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text("providers: []\ngroups: {}\n", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = {"providers": [], "groups": {}}

    with TestClient(main.app) as client:
        response = client.post(
            "/admin/providers/codex-token",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={
                "name": "codex-ui",
                "access_token": "ui-access",
                "refresh_token": "ui-refresh",
                "description": "UI profile",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    provider = main.find_provider("codex-ui")
    assert provider is not None
    assert provider["api_key"] == "ui-access"
    assert {"provider": "codex-ui", "model": "gpt-5.5"} in main.config_data["groups"]["gpt-5.5"]["members"]


def test_admin_logs_page_renders_clickable_modal_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.init_database()
    with sqlite3.connect(main.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)",
            ("test", "test-key", "now"),
        )
        conn.commit()
    main.insert_log(
        "test-key",
        "test",
        "gpt-5.5",
        "gpt-5.5",
        "codex-a",
        "gpt-5.5",
        200,
        "2026-06-13T09:00:00",
        "2026-06-13T09:00:01",
        "2026-06-13T09:00:02",
        1000.0,
        2000.0,
        '[{"role":"user","content":"hi"}]',
        "ok",
        None,
    )

    with TestClient(main.app) as client:
        response = client.get("/admin/logs", auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD))

    assert response.status_code == 200
    assert "Request ID" in response.text
    assert "openLogModal" in response.text
    assert "Exact prompt" in response.text
    assert "Raw sanitized log JSON" in response.text
    assert "log-1" in response.text
    assert "api_key\"" not in response.text
    assert "api_key_hash" in response.text


def test_admin_groups_page_renders_editor(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
providers:
- name: groq
  url: https://api.groq.com/openai/v1
  models:
  - llama-3.1-8b-instant
groups:
  groq-fast:
    description: Groq fallback/fast model
    strategy: fallback
    members:
    - provider: groq
      model: llama-3.1-8b-instant
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()

    with TestClient(main.app) as client:
        response = client.get("/admin/groups", auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD))

    assert response.status_code == 200
    assert "Model Groups" in response.text
    assert "groq-fast" in response.text
    assert "llama-3.1-8b-instant" in response.text
    assert "/admin/groups/groq-fast/save" in response.text
    assert "addMemberRow" in response.text


def test_admin_groups_form_creates_and_saves_group(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
providers:
- name: groq
  url: https://api.groq.com/openai/v1
  models:
  - llama-3.1-8b-instant
  - llama-3.3-70b-versatile
groups: {}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()

    with TestClient(main.app) as client:
        created = client.post(
            "/admin/groups",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={
                "name": "groq-fast",
                "description": "Groq fallback/fast model",
                "strategy": "fallback",
                "member_provider": ["groq"],
                "member_model": ["llama-3.1-8b-instant"],
            },
            follow_redirects=False,
        )
        saved = client.post(
            "/admin/groups/groq-fast/save",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={
                "name": "groq-fast",
                "description": "Updated",
                "strategy": "round_robin",
                "member_provider": ["groq", "groq"],
                "member_model": ["llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
            },
            follow_redirects=False,
        )

    assert created.status_code == 303
    assert saved.status_code == 303
    assert main.config_data["groups"]["groq-fast"] == {
        "description": "Updated",
        "strategy": "round_robin",
        "members": [
            {"provider": "groq", "model": "llama-3.1-8b-instant"},
            {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        ],
    }
    assert "groq-fast" in config_path.read_text(encoding="utf-8")


def test_admin_ollama_form_adds_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text("providers: []\ngroups: {}\n", encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()

    with TestClient(main.app) as client:
        response = client.post(
            "/admin/providers/ollama",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={
                "name": "ollama-local",
                "base_url": "http://host.docker.internal:11434/v1",
                "models": "llama3.2\nqwen2.5-coder:7b",
                "description": "Local Ollama",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    provider = main.find_provider("ollama-local")
    assert provider is not None
    assert provider["url"] == "http://host.docker.internal:11434/v1"
    assert provider["api_key"] == ""
    assert provider["models"] == ["llama3.2", "qwen2.5-coder:7b"]
    assert provider["api_mode"] == "openai_chat_completions"


def test_admin_groups_form_saves_nested_group_member(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
providers:
- name: groq
  url: https://api.groq.com/openai/v1
  models:
  - llama-3.1-8b-instant
groups:
  groq-fast:
    description: Groq fallback/fast model
    strategy: fallback
    members:
    - provider: groq
      model: llama-3.1-8b-instant
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()

    with TestClient(main.app) as client:
        created = client.post(
            "/admin/groups",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={
                "name": "smart-gpt",
                "description": "Nested fallback",
                "strategy": "fallback",
                "member_type": ["group"],
                "member_provider": ["groq"],
                "member_model": ["llama-3.1-8b-instant"],
                "member_group": ["groq-fast"],
            },
            follow_redirects=False,
        )

    assert created.status_code == 303
    assert main.config_data["groups"]["smart-gpt"]["members"] == [{"group": "groq-fast"}]
    assert "group: groq-fast" in config_path.read_text(encoding="utf-8")


def test_admin_playground_page_renders_chat_upload_and_curl_ui(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
providers:
- name: vision-provider
  url: https://vision.example/v1
  api_key: secret
  models:
  - vision-model
groups: {}
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()

    with TestClient(main.app) as client:
        response = client.get("/admin/playground", auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD))

    assert response.status_code == 200
    assert 'enctype="multipart/form-data"' in response.text
    assert 'name="image"' in response.text
    assert "User message" in response.text
    assert "Matching curl request" in response.text or "curlCommand" in response.text
    assert "/admin/playground/run" in response.text
    assert "/admin/playground/jobs/" in response.text


def test_playground_post_accepts_image_and_shows_matching_curl(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
providers:
- name: vision-provider
  url: https://vision.example/v1
  api_key: secret
  models:
  - vision-model
groups: {}
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()

    async def fake_test_provider_model(provider_name, model_name, prompt, image_data_url=""):
        assert provider_name == "vision-provider"
        assert model_name == "vision-model"
        assert prompt == "what is in this image?"
        assert image_data_url.startswith("data:image/png;base64,")
        payload = {"model": model_name, "messages": main.build_playground_messages(prompt, image_data_url)}
        return {
            "success": True,
            "provider": provider_name,
            "status_code": 200,
            "response": '{"choices":[{"message":{"content":"A tiny PNG."}}]}',
            "assistant_text": "A tiny PNG.",
            "curl_command": main.build_curl_command("https://vision.example/v1/chat/completions", payload, True),
        }

    monkeypatch.setattr(main, "test_provider_model", fake_test_provider_model)
    with TestClient(main.app) as client:
        response = client.post(
            "/admin/playground",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={"provider": "vision-provider", "model": "vision-model", "prompt": "what is in this image?"},
            files={"image": ("tiny.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )

    assert response.status_code == 200
    assert "A tiny PNG." in response.text
    assert "tiny.png" in response.text
    assert "curl -sS" in response.text
    assert "image_url" in response.text
    assert "http://testserver/v1/chat/completions" in response.text
    assert "Authorization: Bearer API_KEY" in response.text
    assert "https://vision.example/v1/chat/completions" not in response.text


def test_playground_async_run_returns_job_and_status(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
providers:
- name: vision-provider
  url: https://vision.example/v1
  api_key: secret
  models:
  - vision-model
groups: {}
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()

    async def fake_test_provider_model(provider_name, model_name, prompt, image_data_url=""):
        return {
            "success": True,
            "provider": provider_name,
            "status_code": 200,
            "response": '{"choices":[{"message":{"content":"async ok"}}]}',
            "assistant_text": "async ok",
            "curl_command": main.build_curl_command("https://vision.example/v1/chat/completions", {"model": model_name, "messages": main.build_playground_messages(prompt, image_data_url)}, True),
        }

    monkeypatch.setattr(main, "test_provider_model", fake_test_provider_model)
    with TestClient(main.app) as client:
        started = client.post(
            "/admin/playground/run",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={"provider": "vision-provider", "model": "vision-model", "prompt": "hello"},
            files={"image": ("tiny.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        assert started.status_code == 200
        payload = started.json()
        assert payload["pending"] is True
        assert payload["job_id"]
        assert "curl -sS" in payload["curl_command"]
        assert "http://testserver/v1/chat/completions" in payload["curl_command"]
        assert "Authorization: Bearer API_KEY" in payload["curl_command"]
        assert "https://vision.example/v1/chat/completions" not in payload["curl_command"]
        status_response = client.get(f"/admin/playground/jobs/{payload['job_id']}", auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD))

    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "done"
    assert status_payload["assistant_text"] == "async ok"
    assert status_payload["image_filename"] == "tiny.png"


def test_playground_async_provider_call_is_written_to_logs(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        """
providers:
- name: logged-provider
  url: https://logged.example/v1
  api_key: secret
  models:
  - logged-model
groups: {}
""".strip() + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.config_data = main.load_config()
    main.playground_jobs.clear()

    fake = FakeProviderTestClient()
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "http_client", fake)
        started = client.post(
            "/admin/playground/run",
            auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD),
            data={"provider": "logged-provider", "model": "logged-model", "prompt": "log this playground call"},
            files={"image": ("tiny.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        assert started.status_code == 200
        job_id = started.json()["job_id"]
        completed = client.get(f"/admin/playground/jobs/{job_id}", auth=(main.ADMIN_USERNAME, main.ADMIN_PASSWORD))

    assert completed.status_code == 200
    assert completed.json()["status"] == "done"
    with sqlite3.connect(main.DB_PATH) as conn:
        row = conn.execute(
            "SELECT api_key_name, requested_model, group_name, provider_name, provider_model, status_code, prompt, output, error FROM Logs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert row[0] == "Admin Playground"
    assert row[1] == "logged-model"
    assert row[2] == "admin-playground"
    assert row[3] == "logged-provider"
    assert row[4] == "logged-model"
    assert row[5] == 200
    assert "log this playground call" in row[6]
    assert "[image attached]" in row[6]
    assert "OK" in row[7]
    assert row[8] is None
