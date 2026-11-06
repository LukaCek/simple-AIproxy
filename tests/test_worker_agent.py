"""Offline regression tests for the remote CPU worker lifecycle."""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SIMPLEAI_PROXY_URL", "https://example.test")

# The main proxy CI intentionally does not install optional worker dependencies.
# The dedicated compute-worker CI installs them and runs these tests fully.
pytest.importorskip("huggingface_hub")
pytest.importorskip("websockets.asyncio.client")
from worker import agent


def isolated_worker(tmp_path, monkeypatch, content=b"small deterministic model"):
    models = tmp_path / "models"
    data = tmp_path / "data"
    models.mkdir()
    monkeypatch.setattr(agent, "MODELS", models)
    monkeypatch.setattr(agent, "DATA", data)
    monkeypatch.setattr(agent, "IDENTITY", data / "identity.json")
    monkeypatch.setattr(agent, "CATALOG", {
        "test-model": {
            "repo": "test/repo",
            "file": "model.gguf",
            "revision": "pinned-revision",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "ctx": 4096,
        },
    })
    return agent.Worker()


def test_delete_removes_downloaded_model_and_multi_gb_cache(tmp_path, monkeypatch):
    worker = isolated_worker(tmp_path, monkeypatch)
    expected_cache = agent.MODELS / ".hf-cache" / "test-model"

    def fake_download(**kwargs):
        assert Path(kwargs["cache_dir"]) == expected_cache
        path = expected_cache / "blobs" / "test-blob"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"small deterministic model")
        return str(path)

    monkeypatch.setattr(agent, "hf_hub_download", fake_download)

    class Socket:
        def __init__(self):
            self.messages = []

        async def send(self, msg):
            self.messages.append(json.loads(msg))

    async def run():
        sock = Socket()
        await worker.command(sock, {"id": "install-1", "action": "install", "model": "test-model"})
        installed = agent.model_file("test-model")
        assert installed.is_symlink() and installed.exists()
        assert expected_cache.is_dir()
        assert sock.messages[0] == {"type": "result", "id": "install-1", "ok": True}
        await worker.command(sock, {"id": "delete-1", "action": "delete", "model": "test-model"})
        assert not installed.exists() and not installed.is_symlink()
        assert not expected_cache.exists(), "Deleting the symlink alone leaks model data"
        assert sock.messages[-2] == {"type": "result", "id": "delete-1", "ok": True}

    asyncio.run(run())


def test_invalid_model_checksum_is_not_installed(tmp_path, monkeypatch):
    isolated_worker(tmp_path, monkeypatch)
    def corrupt_download(**kwargs):
        path = Path(kwargs["cache_dir"]) / "blobs" / "bad"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"tampered model")
        return str(path)
    monkeypatch.setattr(agent, "hf_hub_download", corrupt_download)
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        agent.download("test-model")
    assert not agent.model_file("test-model").exists()
    assert not agent.model_file("test-model").is_symlink()


def test_worker_refuses_plaintext_proxy_url(tmp_path, monkeypatch):
    worker = isolated_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "PROXY", "http://example.test")
    with pytest.raises(RuntimeError, match="HTTPS"):
        asyncio.run(worker.run())


def test_published_catalog_pins_revisions_and_integrity():
    import re
    from compute_catalog import CATALOG as published
    assert len(published) >= 4
    for model_id, model in published.items():
        assert re.fullmatch(r"[a-f0-9]{40}", model["revision"]), model_id
        assert re.fullmatch(r"[a-f0-9]{64}", model["sha256"]), model_id
        assert model["size"] > 0, model_id
        assert model["repo"].startswith("unsloth/"), model_id


def test_inference_forwards_tiny_sse_chunks_before_upstream_completes(tmp_path, monkeypatch):
    """SSE must be forwarded immediately, not held until 32768 bytes or EOF."""
    import base64
    from types import SimpleNamespace

    worker = isolated_worker(tmp_path, monkeypatch)
    worker.active = "test-model"
    worker.process = SimpleNamespace(returncode=None)
    release = asyncio.Event()

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self, chunk_size=None):
            assert chunk_size is None, "A fixed chunk size delays small SSE frames"
            yield b'data: {"test":"first"}\n\n'
            await release.wait()
            yield b'data: [DONE]\n\n'

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeClient())

    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()

        async def send(self, raw):
            await self.messages.put(json.loads(raw))

    async def run():
        socket = Socket()
        task = asyncio.create_task(worker.inference(
            socket, {
                "id": "stream-1", "model": "test-model",
                "payload": {"stream": True, "messages": [
                    {"role": "user", "content": "Hello"}
                ]},
            }
        ))
        try:
            first = await asyncio.wait_for(socket.messages.get(), 2)
            assert first["type"] == "chunk", first
            assert base64.b64decode(first["body"]) == b'data: {"test":"first"}\n\n'
        finally:
            release.set()
        await asyncio.wait_for(task, 2)
        second = await asyncio.wait_for(socket.messages.get(), 2)
        end = await asyncio.wait_for(socket.messages.get(), 2)
        assert base64.b64decode(second["body"]) == b'data: [DONE]\n\n'
        assert end == {"type": "end", "id": "stream-1", "status": 200}

    asyncio.run(run())
