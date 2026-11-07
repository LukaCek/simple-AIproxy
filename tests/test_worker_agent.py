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
import compute_worker_storage as storage


def isolated_worker(tmp_path, monkeypatch, content=b"small deterministic model"):
    models = tmp_path / "models"
    data = tmp_path / "data"
    models.mkdir()
    monkeypatch.setattr(agent, "MODELS", models)
    monkeypatch.setattr(agent, "DATA", data)
    monkeypatch.setattr(agent, "IDENTITY", data / "identity.json")
    fake_catalog = {"test-model": {
        "repo": "test/repo",
        "file": "model.gguf",
        "revision": "pinned-revision",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "ctx": 4096,
    }}
    monkeypatch.setattr(agent, "CATALOG", fake_catalog)
    monkeypatch.setattr(storage, "CATALOG", fake_catalog)
    return agent.Worker()


class FakeDownloadResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, chunk_size):
        assert chunk_size > 0
        yield self.content


class FakeDownloadClient:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url):
        assert method == "GET" and "resolve" in url
        return FakeDownloadResponse(self.content)


def mock_model_download(monkeypatch, content):
    monkeypatch.setattr(storage.httpx, "Client", lambda **kwargs: FakeDownloadClient(content))
    monkeypatch.setattr(storage, "hf_hub_url",
                        lambda **kwargs: "https://huggingface.co/test/repo/resolve/pinned-revision/model.gguf")


def test_install_then_delete_reclaims_model_and_legacy_cache(tmp_path, monkeypatch):
    content = b"small deterministic model"
    worker = isolated_worker(tmp_path, monkeypatch, content)
    mock_model_download(monkeypatch, content)

    class Socket:
        def __init__(self):
            self.messages = []

        async def send(self, msg):
            self.messages.append(json.loads(msg))

    async def run():
        sock = Socket()
        await worker.command(sock, {"id": "install-1", "action": "install", "model": "test-model"})
        installed = agent.model_file("test-model")
        assert installed.is_file() and not installed.is_symlink()
        assert installed.read_bytes() == content
        assert sock.messages[0] == {"type": "result", "id": "install-1", "ok": True}
        # Legacy initial installations kept a per-model duplicate HF cache.
        legacy = agent.MODELS / ".hf-cache" / "test-model"
        legacy.mkdir(parents=True)
        (legacy / "blob").write_bytes(content)
        await worker.command(sock, {"id": "delete-1", "action": "delete", "model": "test-model"})
        assert not installed.exists()
        assert not legacy.exists()
        assert sock.messages[-2] == {"type": "result", "id": "delete-1", "ok": True}

    asyncio.run(run())


def test_invalid_model_checksum_is_not_installed(tmp_path, monkeypatch):
    isolated_worker(tmp_path, monkeypatch)
    mock_model_download(monkeypatch, b"tampered model")
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        agent.download("test-model")
    assert not agent.model_file("test-model").exists()
    assert not agent.model_file("test-model").with_suffix(".part").exists()


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


def test_load_defaults_to_reasoning_on(tmp_path, monkeypatch):
    """The llama-server defaults to thinking; the proxy overrides per request."""
    worker = isolated_worker(tmp_path, monkeypatch)
    model = agent.model_file("test-model")
    model.write_bytes(b"small deterministic model")
    monkeypatch.setattr(agent, "REASONING_MODE", "on")
    monkeypatch.setattr(agent, "memory_limit", lambda: 4 * 1024**3)
    monkeypatch.setattr(agent, "memory_available", lambda: 4 * 1024**3)
    commands = []

    class FakeProcess:
        returncode = None

    async def fake_process(*args, **kwargs):
        commands.append(args)
        return FakeProcess()

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr(agent.asyncio, "create_subprocess_exec", fake_process)
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    asyncio.run(worker.load("test-model"))
    assert worker.active == "test-model"
    assert commands
    cmd = commands[0]
    assert cmd[cmd.index("--reasoning") + 1] == "on"


def test_benchmark_metrics_never_invents_unreported_speed():
    data = {"timings": {"prompt_n": 100, "prompt_ms": 2000,
                        "predicted_n": 20, "predicted_ms": 4000}}
    result = agent.benchmark_metrics(data, 6.0)
    assert result["prompt_tps"] == 50.0
    assert result["generation_tps"] == 5.0
    assert result["wall_seconds"] == 6.0
    assert "generation_tps" not in agent.benchmark_metrics({"usage": {"completion_tokens": 20}}, 5.0)
    assert "generation_tps" not in agent.benchmark_metrics(
        {"timings": {"predicted_per_second": float("nan")}}, 1.0)


def test_worker_reports_busy_without_inventing_parallel_capacity(tmp_path, monkeypatch):
    worker = isolated_worker(tmp_path, monkeypatch)
    assert worker.status()["busy"] is False
    assert worker.status()["running_tasks"] == 0
    async def check():
        async with worker.lock:
            assert worker.status()["busy"] is True
            assert worker.status()["running_tasks"] == 1
    asyncio.run(check())



def test_local_tui_snapshot_contains_status_not_prompts_or_credentials(tmp_path, monkeypatch):
    from compute_worker_snapshot import STATUS_FILE, read_snapshot

    worker = isolated_worker(tmp_path, monkeypatch)
    worker._activity("generating", "test-model", "inference")
    current = read_snapshot(agent.DATA / STATUS_FILE)
    assert current["phase"] == "generating"
    assert current["current_model"] == "test-model"
    assert current["current_kind"] == "inference"
    assert current["connected"] is False
    assert current["schema"] == 1
    content = (agent.DATA / STATUS_FILE).read_text()
    for secret_field in ("node_secret", "enrollment_token", "api_key", "payload", "messages"):
        assert secret_field not in content
    worker._activity("idle")
    assert read_snapshot(agent.DATA / STATUS_FILE)["current_model"] is None
