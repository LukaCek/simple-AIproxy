import importlib
import sqlite3
from pathlib import Path

import pytest

import main


@pytest.fixture
def live():
    """Import the runtime entrypoint without leaking its Codex patches to tests."""
    helper_names = (
        "get_default_codex_provider",
        "normalize_config_schema",
        "upsert_codex_profile",
    )
    original_helpers = {
        name: getattr(main, name)
        for name in helper_names
    }
    module = importlib.import_module("live_logs_entrypoint")
    try:
        yield module
    finally:
        for name, helper in original_helpers.items():
            setattr(main, name, helper)


def setup_logs(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.init_database()

    rows = [
        ("first", "2026-01-01T00:00:01"),
        ("second", "2026-01-01T00:00:02"),
        ("third", "2026-01-01T00:00:03"),
    ]
    with sqlite3.connect(db_path) as conn:
        for prompt, created_at in rows:
            conn.execute(
                """
                INSERT INTO Logs (
                    api_key, api_key_name, requested_model, group_name,
                    provider_name, provider_model, status_code, started_at,
                    first_response_at, ended_at, first_response_ms, total_ms,
                    prompt, output, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "key",
                    "test",
                    "strix",
                    "strix",
                    "codex",
                    "gpt-5.6-sol",
                    200,
                    created_at,
                    created_at,
                    created_at,
                    10.0,
                    20.0,
                    prompt,
                    "ok",
                    None,
                    created_at,
                ),
            )
        conn.commit()


def test_list_logs_after_id_returns_only_new_rows_in_order(tmp_path, monkeypatch, live):
    setup_logs(tmp_path, monkeypatch)

    result = live.list_logs_after_id(after_id=1, limit=100)

    assert [row["id"] for row in result["logs"]] == [2, 3]
    assert [row["prompt"] for row in result["logs"]] == ["second", "third"]
    assert result["latest_id"] == 3


def test_list_logs_after_id_honors_limit(tmp_path, monkeypatch, live):
    setup_logs(tmp_path, monkeypatch)

    result = live.list_logs_after_id(after_id=0, limit=1)

    assert [row["id"] for row in result["logs"]] == [1]
    assert result["latest_id"] == 3


def test_live_route_and_browser_client_are_registered(live):
    assert any(
        getattr(route, "path", None) == "/admin/logs/live"
        for route in live.app.router.routes
    )
    assert "/admin/logs/live?after_id=" in live._LIVE_LOGS_SCRIPT
    assert "credentials: 'same-origin'" in live._LIVE_LOGS_SCRIPT
