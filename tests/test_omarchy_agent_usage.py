import json

import omarchy_agent_usage as collector


def test_resolve_api_key_reads_existing_environment_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AIPROXY_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# existing application credentials\nAI_API_KEY='shared-key'\n",
        encoding="utf-8",
    )

    key = collector.resolve_api_key(
        {
            "apiKeyEnvFile": str(env_file),
            "apiKeyEnvName": "AI_API_KEY",
        }
    )

    assert key == "shared-key"


def test_build_record_flattens_all_accounts_and_additional_limits():
    payload = {
        "fetched_at": "2026-09-13T12:00:00+00:00",
        "data": [
            {
                "provider": "codex-a",
                "ok": True,
                "usage": {
                    "plan_type": "plus",
                    "windows": [
                        {
                            "kind": "primary",
                            "label": "5 hours",
                            "used_percent": 25,
                            "reset_iso": "2026-09-13T15:00:00+00:00",
                        },
                        {
                            "kind": "secondary",
                            "label": "Weekly",
                            "used_percent": 40,
                            "reset_iso": "2026-09-19T10:00:00+00:00",
                        },
                    ],
                    "additional_rate_limits": [
                        {
                            "name": "GPT 5.6 Sol",
                            "windows": [
                                {
                                    "kind": "primary",
                                    "label": "5 hours",
                                    "used_percent": 10,
                                    "reset_iso": "2026-09-13T15:00:00+00:00",
                                }
                            ],
                        }
                    ],
                },
            },
            {
                "provider": "codex-b",
                "ok": True,
                "usage": {
                    "plan_type": "plus",
                    "windows": [
                        {
                            "kind": "primary",
                            "label": "5 hours",
                            "used_percent": 75,
                            "reset_iso": "2026-09-13T16:00:00+00:00",
                        }
                    ],
                    "additional_rate_limits": [],
                },
            },
        ],
    }

    record = collector.build_record(
        payload,
        labels={"codex-a": "Luka", "codex-b": "Partner"},
    )

    assert record["id"] == "aiproxy"
    assert record["name"] == "Simple AIproxy"
    assert record["scope"] == "account"
    assert record["tierLabel"] == "2 Codex subscriptions · plus"
    assert record["recentDays"] == []
    assert record["modelUsage"] == {}
    assert [limit["title"] for limit in record["limits"]] == [
        "Luka · Session",
        "Luka · Weekly",
        "Luka · GPT 5.6 Sol · Session",
        "Partner · Session",
    ]
    assert [limit["percent"] for limit in record["limits"]] == [0.25, 0.4, 0.1, 0.75]


def test_build_record_keeps_healthy_account_when_another_fails():
    record = collector.build_record(
        {
            "data": [
                {
                    "provider": "codex-a",
                    "ok": True,
                    "usage": {
                        "windows": [
                            {
                                "kind": "primary",
                                "label": "5 hours",
                                "used_percent": 20,
                                "reset_iso": "",
                            }
                        ]
                    },
                },
                {
                    "provider": "codex-b",
                    "ok": False,
                    "error": {"message": "codex-b requires reauthentication"},
                },
            ]
        }
    )

    assert record["ready"] is True
    assert len(record["limits"]) == 1
    assert record["usageStatusText"] == "1 of 2 subscriptions unavailable"
    assert record["authHelpText"] == "codex-b requires reauthentication"


def test_error_record_stays_visible_without_fake_meter():
    record = collector.error_record("Simple AIproxy API key was rejected")

    assert record["ready"] is False
    assert record["limits"] == [
        {"label": "Unavailable", "percent": -1, "resetsAt": ""}
    ]
    assert record["usageStatusText"] == "Simple AIproxy unavailable"
    assert record["authHelpText"] == "Simple AIproxy API key was rejected"


def test_write_record_is_valid_json_and_sets_public_record_permissions(tmp_path):
    output = tmp_path / "usage" / "aiproxy.json"
    record = collector.error_record("offline")

    collector.write_record(output, record)

    assert json.loads(output.read_text(encoding="utf-8")) == record
    assert output.stat().st_mode & 0o777 == 0o644
    assert not list(output.parent.glob(".aiproxy.json.*"))
