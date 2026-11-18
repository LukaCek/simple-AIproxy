"""Authenticated worker diagnostic transport, privacy, replay and access tests."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from test_zz_compute_plane import ADMIN, enroll_node, fixture_client
from compute_worker_diagnostics import CODES, backend_error_code, describe, error_code, validate

WRITE = {"X-SimpleAI-Admin-Action": "1"}


def record(seq=1, code="ram_limit", level="error", model="qwen35-0.8b-q4"):
    return {
        "type": "diagnostic", "seq": seq,
        "kind": "model_test", "code": code, "level": level,
        "model": model, "occurred_at": int(time.time()),
        "metrics": {"ram_limit_mib": 2048, "ram_available_mib": 422,
                    "disk_limit_mib": 8192, "model_size_mib": 1280},
    }


def test_only_known_structured_fields_can_reach_admin_logs():
    raw = record()
    raw["messages"] = [{"role": "user", "content": "SECRET_PRIVATE_PROMPT"}]
    raw["api_key"] = "sk-SECRET-NEVER-LOG"
    raw["exception"] = "https://example.com/?token=SECRET"
    raw["worker_stderr"] = "private reply"
    clean = validate(raw)
    serialized = repr(clean) + describe(clean)
    assert "SECRET" not in serialized
    assert "private reply" not in serialized
    assert "https://" not in serialized
    assert set(clean) == {"seq", "kind", "code", "level", "model",
                          "occurred_at", "metrics"}
    assert "ram_limit" in CODES


@pytest.mark.parametrize("changed", [
    {"kind": "shell"}, {"code": "credential=sk-secret"},
    {"level": "critical"}, {"model": "../../mysecrets"},
    {"metrics": {"prompt": "private"}},
    {"metrics": {"ram_limit_mib": -1}},
    {"seq": -5}, {"seq": True},
    {"occurred_at": int(time.time()) - 9 * 86400},
])
def test_reject_invalid_and_private_diagnostic_fields(changed):
    with pytest.raises(ValueError):
        validate({**record(), **changed})


def test_exception_classifier_never_returns_raw_text():
    raw = "HTTP 403 download from https://example.com?token=PRIVATE_TOKEN"
    assert error_code(raw) == "download_forbidden"
    assert "PRIVATE_TOKEN" not in error_code(raw)
    assert error_code("Model plus runtime safety margin exceeds configured cgroup RAM") == "ram_limit"
    assert error_code("Exceeds configured model disk limit") == "disk_limit"
    assert error_code("llama-server crashed during startup") == "llama_crashed"


def test_backend_stderr_classifier_discards_private_text():
    raw = b"llama_model: unsupported model architecture gemma4 secret prompt user@email.test"
    assert backend_error_code(raw) == "backend_unsupported"
    assert backend_error_code(b"failed to allocate 4096 MiB") == "backend_out_of_memory"
    assert backend_error_code(b"failed to load model") == "backend_load_failed"
    assert backend_error_code(b"Hello secret input") is None


def test_authenticated_wss_worker_events_are_persisted_without_secrets(tmp_path, monkeypatch):
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        node_id = identity["node_id"]
        assert client.get("/admin/api/compute/events").status_code == 401
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            item = record()
            item["occurred_at"] = int(time.time()) - 120
            item["prompt"] = "NEVER_STORE_THIS_PROMPT"
            item["node_secret"] = "NEVER_STORE_THIS_SECRET"
            ws.send_json(item)
            assert ws.receive_json() == {"type": "diagnostic_ack", "seq": 1}
            # A malformed event is ignored, while the WSS session stays live.
            ws.send_json({**record(seq=2), "code": "raw stderr PRIVATE"})
            ws.send_json(record(seq=3, code="download_not_found"))
            assert ws.receive_json() == {"type": "diagnostic_ack", "seq": 3}
            events = client.get("/admin/api/compute/events",
                                auth=ADMIN, params={"node_id": node_id}).json()["events"]
            diagnostics = [e for e in events if e["event"] == "worker_diagnostic"]
            assert len(diagnostics) == 2
            assert diagnostics[0]["detail"].startswith("model_test/download_not_found")
            assert diagnostics[1]["detail"].startswith("model_test/ram_limit")
            assert diagnostics[1]["status"] == "error"
            assert diagnostics[1]["model"] == "qwen35-0.8b-q4"
            assert "ram_free=422MiB" in diagnostics[1]["detail"]
            assert diagnostics[1]["occurred_at"] == item["occurred_at"]
            assert diagnostics[1]["created_at"] >= item["occurred_at"] + 120
            assert diagnostics[1]["delivery_delay_seconds"] >= 120
            blob = repr(diagnostics)
            assert "NEVER_STORE_THIS" not in blob
            assert "PRIVATE" not in blob
            assert "node_secret" not in blob
        with sqlite3.connect(str(tmp_path / "proxy.db")) as db:
            rows = db.execute("SELECT detail FROM ComputeEvents WHERE event='worker_diagnostic'").fetchall()
            assert len(rows) == 2
            assert all("NEVER" not in row[0] for row in rows)


def test_worker_diagnostic_events_deleted_with_permanent_registration(tmp_path, monkeypatch):
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        node_id = identity["node_id"]
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json(record())
            assert ws.receive_json()["type"] == "diagnostic_ack"
        deleted = client.delete("/admin/api/compute/nodes/" + node_id,
                                auth=ADMIN, headers=WRITE)
        assert deleted.status_code == 200
        events = client.get("/admin/api/compute/events",
                            auth=ADMIN, params={"node_id": node_id}).json()["events"]
        assert events == []


def test_websocket_reconnect_errors_are_classified_without_raw_messages():
    assert error_code("no close frame received or sent") == "transport_error"
    assert error_code("keepalive ping timeout; no close frame received") == "transport_error"
    assert error_code("received 1006 (abnormal closure)") == "transport_error"


def test_websocket_close_codes_are_classified_without_private_reasons():
    from compute_worker_diagnostics import websocket_close_code
    class Close:
        def __init__(self, code): self.code = code
    class FakeDisconnect(Exception):
        def __init__(self, code, message="SECRET_PRIVATE_PROMPT"):
            super().__init__(message)
            self.rcvd = Close(code)
            self.sent = None
    assert websocket_close_code(FakeDisconnect(1000)) == "clean_close"
    assert websocket_close_code(FakeDisconnect(1001)) == "clean_close"
    assert websocket_close_code(FakeDisconnect(1012)) == "server_restart"
    assert websocket_close_code(FakeDisconnect(1006)) == "transport_error"
    assert websocket_close_code(FakeDisconnect(1011)) == "unknown_error"
    assert "SECRET_PRIVATE_PROMPT" not in websocket_close_code(FakeDisconnect(1012))


def test_compute_event_migrates_old_schema_and_preserves_historical_unknown_time(tmp_path, monkeypatch):
    import compute_plane, main_impl as core
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "legacy.db")
    core.init_database()
    with sqlite3.connect(str(core.DB_PATH)) as db:
        db.executescript("""
          CREATE TABLE ComputeEvents (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              node_id TEXT NOT NULL, event TEXT NOT NULL,
              model TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL
          );
        """)
        db.execute("INSERT INTO ComputeEvents(node_id,event,created_at) VALUES(?,?,?)",
                   ("test", "connected", 123))
    compute_plane._setup()
    compute_plane._setup()
    with sqlite3.connect(str(core.DB_PATH)) as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(ComputeEvents)")}
        assert "occurred_at" in cols
        old = db.execute("SELECT created_at,occurred_at FROM ComputeEvents").fetchone()
        assert old == (123, None)


def test_wss_replay_after_lost_ack_does_not_duplicate_events(tmp_path, monkeypatch):
    # A diagnostic can be committed before its ACK is lost on a broken socket.
    # Replaying the same sequence after reconnect must not inflate warnings.
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        item = record(seq=714, code="transport_error", level="warning")
        for _ in range(2):
            with client.websocket_connect("/compute/ws") as ws:
                ws.send_json({"type": "auth", "protocol": 1, **identity})
                assert ws.receive_json()["type"] == "auth_ok"
                ws.send_json(item)
                assert ws.receive_json() == {"type": "diagnostic_ack", "seq": 714}
                ws.send_json(item)
                assert ws.receive_json() == {"type": "diagnostic_ack", "seq": 714}
        with sqlite3.connect(str(tmp_path / "proxy.db")) as db:
            rows = db.execute(
                "SELECT diagnostic_seq,detail FROM ComputeEvents "
                "WHERE node_id=? AND event='worker_diagnostic'",
                (identity["node_id"],)).fetchall()
            assert rows == [(714, "model_test/transport_error ram_cap=2048MiB ram_free=422MiB disk_cap=8192MiB weights=1280MiB")]
