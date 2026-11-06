"""End-to-end control-plane tests without real model downloads or CPU inference."""
import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient

ADMIN = ("admin", "admin")


def fixture_client(tmp_path, monkeypatch):
    import main_impl as core
    import compute_plane
    compute_plane.install(core.app)
    app = core.app
    db = tmp_path / "proxy.db"
    monkeypatch.setattr(core, "DB_PATH", db)
    monkeypatch.setattr(core, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(core, "ADMIN_PASSWORD", "admin")
    core.init_database()
    with sqlite3.connect(str(db)) as conn:
        conn.execute("INSERT INTO API_Keys(name, key, created_at) VALUES('test', 'local-test-key','now')")
    monkeypatch.setattr(core, "config_data", {"providers": [], "groups": {}, "model_profiles": {}})
    return TestClient(app)


def enroll_node(client):
    token_result = client.post("/admin/api/compute/enrollments", auth=ADMIN, headers={"X-SimpleAI-Admin-Action":"1"})
    assert token_result.status_code == 200, token_result.text
    token = token_result.json()["token"]
    registered = client.post("/compute/enroll", json={"token": token, "name": "friend-pc"})
    assert registered.status_code == 200, registered.text
    assert client.post("/compute/enroll", json={"token": token}).status_code == 401
    return registered.json()


def test_enrollment_and_revocation(tmp_path, monkeypatch):
    import compute_plane
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        node = identity["node_id"]
        listed = client.get("/admin/api/compute/nodes", auth=ADMIN, headers={"X-SimpleAI-Admin-Action":"1"}).json()["nodes"]
        assert listed[0]["id"] == node
        assert listed[0]["allow_private"] is False
        assert listed[0]["online"] is False
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json({"type": "telemetry", "hardware": {"arch": "aarch64", "cpus": 4},
                          "models": {"installed": ["qwen35-4b-q4"], "active": "qwen35-4b-q4", "ready": True}})
            import time
            for _ in range(30):
                if compute_plane.endpoints("local/qwen35-4b-q4"):
                    break
                time.sleep(0.02)
            assert compute_plane.endpoints("local/qwen35-4b-q4")
            row = client.get("/admin/api/compute/nodes", auth=ADMIN, headers={"X-SimpleAI-Admin-Action":"1"}).json()["nodes"][0]
            assert row["allow_private"] is False
            assert row["online"] is True
            assert row["hardware"]["arch"] == "aarch64"
            assert client.post(f"/admin/api/compute/nodes/{node}/revoke", auth=ADMIN, headers={"X-SimpleAI-Admin-Action":"1"}).status_code == 200
        assert compute_plane.endpoints("local/qwen35-4b-q4") == []


def test_private_is_not_routed_to_untrusted_node(tmp_path, monkeypatch):
    import compute_plane
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json({"type": "telemetry", "hardware": {"arch": "x86_64"},
                          "models": {"active": "qwen35-4b-q4", "ready": True}})
            import time
            for _ in range(30):
                if compute_plane.endpoints("local/qwen35-4b-q4"):
                    break
                time.sleep(0.02)
            body = {"model": "local/qwen35-4b-q4",
                    "messages": [{"role": "user", "content": "secret prompt"}]}
            result = client.post("/v1/chat/completions",
                                 json=body, headers={"Authorization": "Bearer local-test-key"})
            assert result.status_code in (403, 502)
            assert "not trusted" in result.text.lower()

            # Public data explicitly permits the use of friend's node.
            with ThreadPoolExecutor(max_workers=1) as executor:
                call = executor.submit(
                    client.post, "/v1/chat/completions",
                    json={**body, "aiproxy_visibility": "public"},
                    headers={"Authorization": "Bearer local-test-key"},
                )
                run = ws.receive_json()
                assert run["type"] == "run"
                assert run["payload"].get("aiproxy_visibility") is None
                assert run["payload"]["messages"][0]["content"] == "secret prompt"
                reply = {"id": "fake", "object": "chat.completion", "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
                ws.send_json({"type": "chunk", "id": run["id"],
                              "body": base64.b64encode(json.dumps(reply).encode()).decode()})
                ws.send_json({"type": "end", "id": run["id"], "status": 200})
                result = call.result(timeout=10)
            assert result.status_code == 200
            assert result.json()["choices"][0]["message"]["content"] == "OK"


def test_catalog_rejects_unknown_model(tmp_path, monkeypatch):
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        result = client.post(
            f"/admin/api/compute/nodes/{identity['node_id']}/operations",
            auth=ADMIN, headers={"X-SimpleAI-Admin-Action":"1"},
            json={"action": "install", "model": "not-in-catalog"},
        )
        assert result.status_code == 400
        assert client.post(
            f"/admin/api/compute/nodes/{identity['node_id']}/trust",
            auth=ADMIN, headers={"X-SimpleAI-Admin-Action": "1"}, json={"allow_private": "yes"},
        ).status_code == 400


def test_trust_revocation_blocks_stale_routed_endpoint(tmp_path, monkeypatch):
    """Queued/routed endpoint snapshots must never bypass a later admin decision."""
    import asyncio
    import time
    from fastapi import HTTPException
    import compute_plane

    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        node = identity["node_id"]
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json({"type": "telemetry", "hardware": {"arch": "x86_64"},
                          "models": {"active": "qwen35-2b-q4", "ready": True}})
            for _ in range(40):
                if compute_plane.endpoints("local/qwen35-2b-q4"):
                    break
                time.sleep(0.025)
            assert compute_plane.endpoints("local/qwen35-2b-q4")
            response = client.post(f"/admin/api/compute/nodes/{node}/trust",
                                   auth=ADMIN, headers={"X-SimpleAI-Admin-Action": "1"},
                                   json={"allow_private": True})
            assert response.status_code == 200
            stale_trusted = compute_plane.endpoints("local/qwen35-2b-q4")[0]
            assert stale_trusted["allow_private"] is True

            response = client.post(f"/admin/api/compute/nodes/{node}/trust",
                                   auth=ADMIN, headers={"X-SimpleAI-Admin-Action": "1"},
                                   json={"allow_private": False})
            assert response.status_code == 200
            try:
                asyncio.run(compute_plane.forward(stale_trusted, {"messages": [{"role": "user", "content": "private"}]}))
                assert False, "Private request unexpectedly reached revoked trust"
            except HTTPException as exc:
                assert exc.status_code == 403 and "not trusted" in exc.detail

            response = client.post(f"/admin/api/compute/nodes/{node}/revoke",
                                   auth=ADMIN, headers={"X-SimpleAI-Admin-Action": "1"})
            assert response.status_code == 200
            try:
                asyncio.run(compute_plane.forward(stale_trusted, {
                    "aiproxy_visibility": "public", "messages": [{"role": "user", "content": "public"}]
                }))
                assert False, "Revoked node unexpectedly received a public request"
            except HTTPException as exc:
                assert exc.status_code == 403 and "revoked" in exc.detail


def test_untrusted_telemetry_cannot_crash_dashboard(tmp_path, monkeypatch):
    import time
    import compute_plane
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json({
                "type": "telemetry",
                "hardware": {"arch": "x86_64", "memory_limit_bytes": "nonsense",
                             "memory_available_bytes": None, "disk_free_bytes": True},
                "models": {"active": "qwen35-2b-q4", "ready": False, "installed": "not-a-list"},
            })
            for _ in range(40):
                data = client.get("/admin/api/compute/nodes", auth=ADMIN).json()["nodes"]
                if data[0]["hardware"].get("arch") == "x86_64":
                    break
                time.sleep(0.025)
            assert data[0]["hardware"]["memory_limit_bytes"] == 0
            assert data[0]["models"]["installed"] == []
            rec = client.get(
                f"/admin/api/compute/nodes/{identity['node_id']}/recommendations", auth=ADMIN
            )
            assert rec.status_code == 200, rec.text
            assert rec.json()["models"]["qwen35-2b-q4"]["grade"] == "unknown"
            html = client.get("/admin/compute-nodes", auth=ADMIN)
            assert html.status_code == 200, html.text
