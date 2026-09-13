import base64
import json
import sqlite3
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

import codex_usage_entrypoint as usage
import main


def _jwt(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_codex_account_id_from_nested_jwt_claim():
    provider = {
        "access_token": _jwt(
            {
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "account-test-123"
                }
            }
        )
    }
    assert usage.codex_account_id(provider) == "account-test-123"


def test_normalize_codex_usage_windows_and_reset():
    now = int(time.time())
    normalized = usage.normalize_codex_usage(
        {
            "plan_type": "plus",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {
                    "used_percent": 25,
                    "limit_window_seconds": 18000,
                    "reset_at": now + 3600,
                },
                "secondary_window": {
                    "used_percent": 40,
                    "limit_window_seconds": 604800,
                    "reset_at": now + 86400,
                },
            },
            "credits": {"has_credits": True, "balance": "0"},
        }
    )

    assert normalized["plan_type"] == "plus"
    assert normalized["limit_reached"] is False
    assert normalized["windows"][0]["label"] == "5 hours"
    assert normalized["windows"][0]["remaining_percent"] == 75.0
    assert normalized["windows"][1]["label"] == "Weekly"
    assert normalized["windows"][1]["remaining_percent"] == 60.0
    assert normalized["windows"][0]["reset_iso"]


def test_normalize_app_server_style_window_names():
    normalized = usage.normalize_codex_usage(
        {
            "rate_limit": {
                "primary": {
                    "usedPercent": 12,
                    "windowDurationMins": 300,
                    "resetsAt": 1800000000,
                }
            }
        }
    )
    assert normalized["windows"][0]["label"] == "5 hours"
    assert normalized["windows"][0]["remaining_percent"] == 88.0


def _setup_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "app.db")
    main.init_database()
    with main.get_db_connection() as conn:
        conn.execute(
            "INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)",
            ("omarchy", "usage-key", "now"),
        )
        conn.commit()


def _codex_provider(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "is_codex_oauth": True,
    }


def test_public_usage_endpoint_requires_proxy_api_key(tmp_path, monkeypatch):
    _setup_api_key(tmp_path, monkeypatch)
    client = TestClient(usage.app)

    response = client.get("/v1/codex/usage")

    assert response.status_code == 401


def test_public_usage_endpoint_returns_all_live_profiles(tmp_path, monkeypatch):
    _setup_api_key(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main,
        "get_providers",
        lambda: [
            _codex_provider("codex-luka", "Luka"),
            _codex_provider("codex-partner", "Partner"),
            {"name": "ollama", "is_codex_oauth": False},
        ],
    )

    async def fake_fetch(provider_name: str) -> dict:
        return {
            "provider": provider_name,
            "plan_type": "plus",
            "windows": [{"label": "5 hours", "used_percent": 25}],
        }

    monkeypatch.setattr(usage, "fetch_codex_usage", fake_fetch)
    client = TestClient(usage.app)

    response = client.get(
        "/v1/codex/usage",
        headers={"Authorization": "Bearer usage-key"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["object"] == "codex.usage.list"
    assert [item["provider"] for item in payload["data"]] == [
        "codex-luka",
        "codex-partner",
    ]
    assert [item["name"] for item in payload["data"]] == ["Luka", "Partner"]
    assert all(item["ok"] for item in payload["data"])

    with sqlite3.connect(main.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM Logs").fetchone()[0] == 0


def test_public_usage_endpoint_keeps_partial_failures(tmp_path, monkeypatch):
    _setup_api_key(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main,
        "get_providers",
        lambda: [
            _codex_provider("codex-luka", "Luka"),
            _codex_provider("codex-partner", "Partner"),
        ],
    )

    async def fake_fetch(provider_name: str) -> dict:
        if provider_name == "codex-partner":
            raise HTTPException(status_code=401, detail="upstream response")
        return {"provider": provider_name, "plan_type": "plus", "windows": []}

    monkeypatch.setattr(usage, "fetch_codex_usage", fake_fetch)
    client = TestClient(usage.app)

    response = client.get(
        "/v1/codex/usage",
        headers={"Authorization": "Bearer usage-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"][0]["ok"] is True
    assert payload["data"][1] == {
        "provider": "codex-partner",
        "name": "Partner",
        "ok": False,
        "error": {
            "status": 401,
            "message": "Codex profile 'codex-partner' requires reauthentication",
        },
    }
