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
                    json={**body, "aiproxy_visibility": "public", "think": False},
                    headers={"Authorization": "Bearer local-test-key"},
                )
                run = ws.receive_json()
                assert run["type"] == "run"
                assert run["payload"].get("aiproxy_visibility") is None
                assert run["payload"].get("think") is None
                assert run["payload"]["chat_template_kwargs"]["enable_thinking"] is False
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


def test_real_wss_sse_chunk_passthrough(tmp_path, monkeypatch):
    """Public SSE traffic crosses the actual proxy websocket, byte-for-byte."""
    import time
    import compute_plane

    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            ws.send_json({"type": "telemetry", "hardware": {"arch": "x86_64"},
                          "models": {"active": "qwen35-0.8b-q4", "ready": True}})
            for _ in range(50):
                if compute_plane.endpoints("local/qwen35-0.8b-q4"):
                    break
                time.sleep(0.02)
            assert compute_plane.endpoints("local/qwen35-0.8b-q4")
            expected = (
                b'data: {"id":"stream","choices":[{"index":0,"delta":{"content":"Hello"}}]}'
                + bytes((10, 10)) + b'data: [DONE]' + bytes((10, 10))
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    client.post, "/v1/chat/completions",
                    json={"model": "local/qwen35-0.8b-q4", "stream": True,
                          "aiproxy_visibility": "public",
                          "messages": [{"role": "user", "content": "Public hello"}]},
                    headers={"Authorization": "Bearer local-test-key"},
                )
                run = ws.receive_json()
                assert run["type"] == "run"
                assert run["payload"]["stream"] is True
                assert "aiproxy_visibility" not in run["payload"]
                for part in (expected[:19], expected[19:]):
                    ws.send_json({"type": "chunk", "id": run["id"],
                                  "body": base64.b64encode(part).decode()})
                ws.send_json({"type": "end", "id": run["id"], "status": 200})
                result = future.result(timeout=15)
            assert result.status_code == 200, result.text
            assert result.content == expected


def test_streaming_consent_is_rechecked_at_first_byte(tmp_path, monkeypatch):
    """No private prompt leaves the server after consent is removed."""
    import asyncio
    from fastapi import HTTPException
    import compute_plane

    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        node = identity["node_id"]
        response = client.post(
            f"/admin/api/compute/nodes/{node}/trust", auth=ADMIN,
            headers={"X-SimpleAI-Admin-Action": "1"}, json={"allow_private": True})
        assert response.status_code == 200
        candidate = {"node_id": node, "model": "qwen35-0.8b-q4", "allow_private": True}

        async def exercise():
            prepared = await compute_plane.forward(candidate, {
                "stream": True, "messages": [{"role": "user", "content": "secret"}]
            })
            denied = client.post(
                f"/admin/api/compute/nodes/{node}/trust", auth=ADMIN,
                headers={"X-SimpleAI-Admin-Action": "1"},
                json={"allow_private": False},
            )
            assert denied.status_code == 200
            try:
                await anext(prepared.body_iterator)
            except HTTPException as exc:
                assert exc.status_code == 403 and "not trusted" in exc.detail
            else:
                assert False, "Streaming sent a prompt after private permission was removed"

        asyncio.run(exercise())


def test_local_cpu_worker_respects_small_token_limit():
    import main_impl as core

    original = {"max_tokens": 64, "stream": True}
    local = core.prepare_provider_chat_payload(
        original, {"api_mode": "compute_worker"}, "qwen35-0.8b-q4"
    )
    assert local["max_tokens"] == 64, "Never inflate CPU worker's short test"
    upstream = core.prepare_provider_chat_payload(
        original, {"api_mode": "openai_chat_completions"}, "other-model"
    )
    assert upstream["max_tokens"] == core.MIN_COMPLETION_TOKENS
    assert original["max_tokens"] == 64


def test_proxy_thinking_defaults_on_and_explicit_override():
    """Every thinking request has a budget and reserves response tokens."""
    import compute_plane
    from fastapi import HTTPException
    import pytest

    body = {"messages": [{"role": "user", "content": "Hello"}],
            "stream": True, "max_tokens": 512}
    original = body.copy()
    implicit = compute_plane.prepare_worker_thinking(body)
    assert implicit["reasoning_budget_tokens"] == 128
    assert implicit["max_tokens"] == 512
    assert implicit is not body
    assert body == original

    explicit_on = compute_plane.prepare_worker_thinking({**body, "think": True})
    explicit_off = compute_plane.prepare_worker_thinking({**body, "think": False})
    assert explicit_on["chat_template_kwargs"]["enable_thinking"] is True
    assert explicit_on["reasoning_budget_tokens"] == 128
    assert explicit_off["chat_template_kwargs"]["enable_thinking"] is False
    assert "reasoning_budget_tokens" not in explicit_off
    assert "think" not in explicit_on and "think" not in explicit_off

    # No max_tokens: still apply a bounded default so CPU requests do not spin.
    assert compute_plane.prepare_worker_thinking({"messages": []})[
        "reasoning_budget_tokens"] == 192

    # Explicit settings win over template defaults without restarting llama.cpp.
    priority = compute_plane.prepare_worker_thinking({
        **body, "think": False, "chat_template_kwargs": {"enable_thinking": True, "foo": 1},
    })
    assert priority["chat_template_kwargs"] == {"enable_thinking": False, "foo": 1}

    assert compute_plane.prepare_worker_thinking({
        **body, "think": True,
        "chat_template_kwargs": {"enable_thinking": False}
    })["reasoning_budget_tokens"] == 128
    assert "reasoning_budget_tokens" not in compute_plane.prepare_worker_thinking({
        **body, "chat_template_kwargs": {"enable_thinking": False}
    })

    assert compute_plane.prepare_worker_thinking({
        **body, "think": True, "reasoning_budget_tokens": 96,
    })["reasoning_budget_tokens"] == 96
    assert compute_plane.prepare_worker_thinking({
        **body, "thinking_budget_tokens": 120,
    })["reasoning_budget_tokens"] == 120
    assert compute_plane.prepare_worker_thinking({
        **body, "max_tokens": 96,
    })["reasoning_budget_tokens"] == 24
    assert compute_plane.prepare_worker_thinking({
        **body, "max_tokens": 2000,
    })["reasoning_budget_tokens"] == 256

    # OpenAI reasoning_effort=none is supported by pinned llama.cpp.
    assert "reasoning_budget_tokens" not in compute_plane.prepare_worker_thinking({
        **body, "reasoning_effort": "none",
    })

    for invalid in (None, "false", 0, 1, [], {}):
        with pytest.raises(HTTPException) as err:
            compute_plane.prepare_worker_thinking({**body, "think": invalid})
        assert err.value.status_code == 400
    for invalid in (-1, 512, "128", True):
        with pytest.raises(HTTPException) as err:
            compute_plane.prepare_worker_thinking({
                **body, "think": True, "reasoning_budget_tokens": invalid,
            })
        assert err.value.status_code == 400
    with pytest.raises(HTTPException):
        compute_plane.prepare_worker_thinking({
            **body, "think": True, "reasoning_effort": "none",
        })
    with pytest.raises(HTTPException):
        compute_plane.prepare_worker_thinking({
            **body, "think": False, "chat_template_kwargs": "not-an-object",
        })
    with pytest.raises(HTTPException):
        compute_plane.prepare_worker_thinking({
            **body, "thinking_budget_tokens": 32, "reasoning_budget_tokens": 64,
        })


def test_admin_model_policy_pushes_wss_and_routes_verified_cached_models(tmp_path, monkeypatch):
    """Select models at proxy only; a verified model routes even if not loaded."""
    import time
    import compute_plane

    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        write = {"X-SimpleAI-Admin-Action": "1"}
        policy_url = "/admin/api/compute/model-policy"
        assert client.get(policy_url, auth=ADMIN).json()["models"] == []
        assert client.post(policy_url, auth=ADMIN, headers=write,
                           json={"models": ["unknown-id"]}).status_code == 400
        assert client.post(policy_url, auth=ADMIN, headers=write,
                           json={"models": ["qwen35-0.8b-q4"] * 2}).status_code == 400
        assert client.post(policy_url, auth=ADMIN,
                           json={"models": ["qwen35-0.8b-q4"]}).status_code == 403

        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            saved = client.post(
                policy_url, auth=ADMIN, headers=write,
                json={"models": ["qwen35-0.8b-q4", "qwen35-2b-q4"]},
            )
            assert saved.status_code == 200, saved.text
            message = ws.receive_json()
            assert message["type"] == "policy"
            assert message["models"] == ["qwen35-0.8b-q4", "qwen35-2b-q4"]
            ws.send_json({"type": "telemetry",
                          "hardware": {"cpus": 2, "memory_limit_bytes": 2*1024**3},
                          "models": {
                              "installed": ["qwen35-0.8b-q4"],
                              "active": "qwen35-0.8b-q4", "ready": True,
                              "disk_used_bytes": 500000000,
                              "disk_limit_bytes": 2*1024**3,
                              "capabilities": {
                                  "qwen35-0.8b-q4": {"status": "supported",
                                      "reason": "Actual inference passed"},
                                  "qwen35-2b-q4": {"status": "supported",
                                      "reason": "Passed, unloaded"},
                              },
                          }})
            for _ in range(100):
                if compute_plane.endpoints("local/qwen35-2b-q4"):
                    break
                time.sleep(0.02)
            endpoints = compute_plane.endpoints("local/qwen35-2b-q4")
            assert len(endpoints) == 1
            assert endpoints[0]["model"] == "qwen35-2b-q4"
            nodes = client.get("/admin/api/compute/nodes", auth=ADMIN).json()["nodes"]
            assert nodes[0]["models"]["capabilities"]["qwen35-2b-q4"]["status"] == "supported"
            assert nodes[0]["models"]["disk_limit_bytes"] == 2*1024**3

            # A supported but currently unloaded model is exposed to routing,
            # but it never receives a private prompt on an untrusted friend PC.
            private = client.post(
                "/v1/chat/completions",
                json={"model": "local/qwen35-2b-q4",
                      "messages": [{"role": "user", "content": "private secret"}]},
                headers={"Authorization": "Bearer local-test-key"},
            )
            assert private.status_code in (403, 502)
            assert "not trusted" in private.text.lower()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    client.post, "/v1/chat/completions",
                    json={"model": "local/qwen35-2b-q4",
                          "aiproxy_visibility": "public",
                          "messages": [{"role": "user", "content": "public request"}]},
                    headers={"Authorization": "Bearer local-test-key"},
                )
                run = ws.receive_json()
                assert run["type"] == "run"
                assert run["model"] == "qwen35-2b-q4"
                assert run["payload"]["messages"][0]["content"] == "public request"
                reply = {"id": "chatcmpl-test", "object": "chat.completion",
                         "choices": [{"index": 0, "message":
                          {"role": "assistant", "content": "Ready"}, "finish_reason": "stop"}]}
                ws.send_json({"type": "chunk", "id": run["id"],
                              "body": base64.b64encode(json.dumps(reply).encode()).decode()})
                ws.send_json({"type": "end", "id": run["id"], "status": 200})
                answer = future.result(timeout=10)
            assert answer.status_code == 200, answer.text
            assert answer.json()["choices"][0]["message"]["content"] == "Ready"

            # Clearing the desired list disables *all* managed model routes.
            cleared = client.post(policy_url, auth=ADMIN, headers=write,
                                  json={"models": []})
            assert cleared.status_code == 200
            update_message = ws.receive_json()
            if update_message.get("type") == "cancel":
                update_message = ws.receive_json()
            assert update_message["type"] == "policy"
            assert update_message["models"] == []
            assert compute_plane.endpoints("local/qwen35-2b-q4") == []
            assert compute_plane.endpoints("local/qwen35-0.8b-q4") == []


def test_automatic_policy_moves_models_to_node_fit_and_resolves_exact_model(tmp_path, monkeypatch):
    """Full admin -> WSS telemetry -> per-node policy -> auto routing path."""
    import compute_plane
    from compute_catalog import CATALOG
    selected = ["qwen35-9b-q4", "qwen35-4b-q4", "qwen35-2b-q4", "qwen35-0.8b-q4"]
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        headers = {"X-SimpleAI-Admin-Action": "1"}
        response = client.post("/admin/api/compute/model-policy",
                               auth=ADMIN, headers=headers, json={"models": selected})
        assert response.status_code == 200, response.text
        mode = client.post("/admin/api/compute/placement",
                           auth=ADMIN, headers=headers, json={"mode": "auto"})
        assert mode.status_code == 200 and mode.json()["mode"] == "auto"
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            # Before first telemetry, the safe baseline is the global list.
            first_policy = ws.receive_json()
            assert first_policy["models"] == selected
            ws.send_json({
                "type": "telemetry",
                "hardware": {"memory_limit_bytes": 8 * 1024**3, "cpus": 4},
                "models": {
                    "disk_limit_bytes": 8 * 1024**3, "installed": selected,
                    "capabilities": {mid: {"status": "supported"} for mid in selected},
                    "active": "qwen35-4b-q4", "ready": True,
                },
            })
            optimized = ws.receive_json()
            assert optimized["models"] == ["qwen35-4b-q4", "qwen35-2b-q4"]
            quality = compute_plane.automatic_endpoints({"aiproxy_compute_tier": "quality"})
            assert len(quality) == 1 and quality[0]["model"] == "qwen35-4b-q4"
            fast = compute_plane.automatic_endpoints({"aiproxy_compute_tier": "fast"})
            assert len(fast) == 1 and fast[0]["model"] == "qwen35-2b-q4"
            assert client.get("/admin/api/compute/placement", auth=ADMIN).json()["mode"] == "auto"

