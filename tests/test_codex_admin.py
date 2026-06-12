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
