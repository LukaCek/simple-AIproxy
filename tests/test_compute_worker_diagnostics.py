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
