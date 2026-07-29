import os
import subprocess
import sys
from pathlib import Path


def run_isolated_live_test(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_live_logs_and_running_requests_are_isolated(tmp_path):
    db_path = tmp_path / "app.db"
    script = f'''
import sqlite3
from pathlib import Path

import main

main.DB_PATH = Path({str(db_path)!r})
main.init_database()

import live_logs_entrypoint as live

with sqlite3.connect(main.DB_PATH) as conn:
    rows = [
        ("first", "2026-01-01T00:00:01"),
        ("second", "2026-01-01T00:00:02"),
        ("third", "2026-01-01T00:00:03"),
    ]
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
                "key", "test", "strix", "strix", "codex", "gpt-5.6-sol",
                200, created_at, created_at, created_at, 10.0, 20.0,
                prompt, "ok", None, created_at,
            ),
        )
    conn.commit()

result = live.list_logs_after_id(after_id=1, limit=100)
assert [row["id"] for row in result["logs"]] == [2, 3]
assert [row["prompt"] for row in result["logs"]] == ["second", "third"]
assert result["latest_id"] == 3
assert result["running"] == []

live._active_provider = lambda payload: ("codex", "gpt-5.6-sol")
request_id = live.start_active_request(
    {{
        "model": "strix",
        "messages": [{{"role": "user", "content": "scan this target"}}],
    }},
    request_id="active-test",
)
assert request_id == "active-test"
running = live.list_active_requests()
assert len(running) == 1
assert running[0]["request_id"] == "active-test"
assert running[0]["running"] is True
assert running[0]["status"] == "running"
assert running[0]["provider_name"] == "codex"
assert running[0]["provider_model"] == "gpt-5.6-sol"
assert "scan this target" in running[0]["prompt"]

response = live.list_logs_after_id(after_id=0, limit=100)
assert [row["request_id"] for row in response["running"]] == ["active-test"]

live.finish_active_request("active-test")
assert live.list_active_requests() == []

assert any(
    getattr(route, "path", None) == "/admin/logs/live"
    for route in live.app.router.routes
)
assert "/admin/logs/live?after_id=" in live._LIVE_LOGS_SCRIPT
assert "credentials: 'same-origin'" in live._LIVE_LOGS_SCRIPT
assert "Running" in live._LIVE_LOGS_SCRIPT
assert "data.running" in live._LIVE_LOGS_SCRIPT
'''
    result = run_isolated_live_test(tmp_path, script)
    assert result.returncode == 0, result.stdout + result.stderr
