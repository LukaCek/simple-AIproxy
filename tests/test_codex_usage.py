import base64
import json
import time

import codex_usage_entrypoint as usage


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
