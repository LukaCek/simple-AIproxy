"""Compute worker Playground integration and explicit prompt visibility tests."""
import asyncio
import json

import pytest
from fastapi import HTTPException, Response


def test_connected_verified_compute_models_are_listed_only_when_available(monkeypatch):
    import main_impl as core
    import compute_plane
    monkeypatch.setattr(compute_plane, "endpoints", lambda model: (
        [{"api_mode":"compute_worker", "model":model.split("/")[-1],
          "name":"compute-node", "allow_private":False, "node_id":"test-node"}]
        if model == "local/qwen35-0.8b-q4" else []))
    assert core.compute_playground_models() == ["local/qwen35-0.8b-q4", "local/auto"]
    assert core.parse_playground_target("compute:local") == ("compute", "local")
    assert "local/qwen35-0.8b-q4" in core.build_playground_curl_for_target(
        "compute:local", "local/qwen35-0.8b-q4", "Hello")
    default = core.build_playground_curl_for_target(
        "compute:local", "local/qwen35-0.8b-q4", "Hello")
    assert "aiproxy_visibility" not in default
    public = core.build_playground_curl_for_target(
        "compute:local", "local/qwen35-0.8b-q4", "Hello", visibility="public")
    assert "aiproxy_visibility" in public
    assert "public" in public


def test_untrusted_compute_worker_cannot_receive_private_playground_prompt(monkeypatch):
    import main_impl as core
    import compute_plane
    candidate = {"api_mode":"compute_worker", "model":"qwen35-0.8b-q4",
                 "name":"untrusted-node", "allow_private":False, "node_id":"node-1"}
    monkeypatch.setattr(compute_plane, "endpoints",
        lambda model: [candidate] if model == "local/qwen35-0.8b-q4" else [])
    forwarded = []

    async def forward(endpoint, payload):
        forwarded.append(payload)
        return Response(content=json.dumps({"choices":[{"message":{"content":"Hello!"}}]}),
                        media_type="application/json")
    monkeypatch.setattr(compute_plane, "forward", forward)

    async def run():
        with pytest.raises(HTTPException) as e:
            await core.test_compute_playground_model("local", "local/qwen35-0.8b-q4", "secret")
        assert e.value.status_code == 403
        assert not forwarded
        result = await core.test_compute_playground_model(
            "local", "local/qwen35-0.8b-q4", "public demo", visibility="public")
        assert result["success"] and result["assistant_text"] == "Hello!"
        assert forwarded[0]["aiproxy_visibility"] == "public"
        with pytest.raises(HTTPException) as image_error:
            await core.test_compute_playground_model(
                "local", "local/qwen35-0.8b-q4", "hello", image="data:image/png;base64,xxx")
        assert image_error.value.status_code == 400
    asyncio.run(run())


def test_compute_playground_does_not_accept_arbitrary_models(monkeypatch):
    import compute_plane
    import main_impl as core
    monkeypatch.setattr(compute_plane, "endpoints", lambda _model: [])
    assert core.compute_playground_models() == []
    with pytest.raises(HTTPException) as exc:
        core.build_playground_curl_for_target("compute:local", "local/nonexistent", "Hi")
    assert exc.value.status_code == 404
