"""Isolated production adapter test; executed in its own pytest process."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse
import main_impl as core
from full_entrypoint import app
import compute_plane


def test_true_streaming_production_route(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "proxy.db")
    core.init_database()
    with sqlite3.connect(str(core.DB_PATH)) as db:
        db.execute("INSERT INTO API_Keys(name,key,created_at) VALUES('ci','stream-test','now')")
    monkeypatch.setattr(core, "config_data", {"providers": [], "groups": {}, "model_profiles": {}})
    seen = []

    def fake_nodes(model):
        if model != "local/qwen35-2b-q4":
            return []
        return [{"name": "compute-test", "api_mode": "compute_worker",
                 "node_id": "fake", "model": "qwen35-2b-q4", "allow_private": False}]

    async def fake_forward(endpoint, payload):
        seen.append((endpoint["model"], payload.get("aiproxy_visibility"), payload["messages"]))
        async def produce():
            yield b'data: {"id":"test","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
            yield b'data: [DONE]\n\n'
        return StreamingResponse(produce(), media_type="text/event-stream")

    monkeypatch.setattr(compute_plane, "endpoints", fake_nodes)
    monkeypatch.setattr(compute_plane, "forward", fake_forward)
    request = {"model": "local/qwen35-2b-q4", "stream": True,
               "aiproxy_visibility": "public",
               "messages": [{"role": "user", "content": "Public hello"}]}
    with TestClient(app) as client:
        result = client.post("/v1/chat/completions",
                             headers={"Authorization": "Bearer stream-test"}, json=request)
        assert result.status_code == 200, result.text
        assert "Hello" in result.text and "[DONE]" in result.text
    assert seen == [("qwen35-2b-q4", "public", request["messages"])]
