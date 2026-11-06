"""Dedicated, process-isolated production streaming bridge test.
This file deliberately is not named test_*.py: the existing suite imports the
base app first and mutates routes; run this check in a separate pytest process.
"""
import base64
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import main_impl as core
from full_entrypoint import app
import compute_plane


def test_true_streaming_through_production_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "proxy.db")
    monkeypatch.setattr(core, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(core, "ADMIN_PASSWORD", "admin")
    core.init_database()
    with sqlite3.connect(str(core.DB_PATH)) as db:
        db.execute("INSERT INTO API_Keys(name,key,created_at) VALUES('ci','stream-test','now')")
    monkeypatch.setattr(core, "config_data", {"providers": [], "groups": {}, "model_profiles": {}})
    auth = ("admin", "admin")
    headers = {"X-SimpleAI-Admin-Action": "1"}
    with TestClient(app) as client:
        token = client.post("/admin/api/compute/enrollments", auth=auth, headers=headers).json()["token"]
        identity = client.post("/compute/enroll", json={"token": token, "name": "sandbox"}).json()
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json({"type": "telemetry", "hardware": {"arch": "aarch64"},
                          "models": {"ready": True, "active": "qwen35-2b-q4"}})
            for _ in range(30):
                if compute_plane.endpoints("local/qwen35-2b-q4"):
                    break
                time.sleep(.02)
            assert compute_plane.endpoints("local/qwen35-2b-q4")
            payload = {
                "model": "local/qwen35-2b-q4",
                "messages": [{"role": "user", "content": "public greeting"}],
                "stream": True,
                "aiproxy_visibility": "public",
            }
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(client.post, "/v1/chat/completions",
                                     headers={"Authorization": "Bearer stream-test"}, json=payload)
                run = ws.receive_json()
                assert run["type"] == "run"
                assert run["payload"]["stream"] is True
                sse = 'data: {"id":"test","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
                sse += "data: [DONE]\n\n"
                ws.send_json({"type": "chunk", "id": run["id"],
                              "body": base64.b64encode(sse.encode()).decode()})
                ws.send_json({"type": "end", "id": run["id"], "status": 200})
                result = future.result(timeout=10)
            assert result.status_code == 200
            assert "Hello" in result.text
            assert "[DONE]" in result.text
