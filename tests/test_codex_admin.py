import sqlite3
import sys
from pathlib import Path

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
