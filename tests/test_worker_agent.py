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
