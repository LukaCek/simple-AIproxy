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


def test_large_manual_load_fails_before_evicting_working_model(tmp_path, monkeypatch):
    """An impossible 4B model must not erase smaller cached GGUF files."""
    worker = isolated_worker(tmp_path, monkeypatch)
    small = agent.model_file("test-model")
    small.write_bytes(b"small deterministic model")
    monkeypatch.setattr(agent, "memory_limit", lambda: 3 * 1024**3)
    monkeypatch.setattr(agent, "memory_available", lambda: 3 * 1024**3)
    agent.CATALOG["oversized"] = {
        "repo": "test/large", "file": "huge.gguf", "size": 2_740_000_000,
        "sha256": "a" * 64, "ctx": 4096,
    }
    class Socket:
        def __init__(self):
            self.messages = []
        async def send(self, raw):
            self.messages.append(json.loads(raw))

    sock = Socket()
    asyncio.run(worker.command(sock, {"id":"cannot-load", "action":"load", "model":"oversized"}))
    assert small.exists()
    assert sock.messages[0]["type"] == "error"
    assert "RAM limit" in sock.messages[0]["error"]
    assert not (agent.MODELS / "oversized.gguf").exists()


def test_worker_refreshes_local_snapshot_each_second_but_not_wss_telemetry(monkeypatch, tmp_path):
    worker = isolated_worker(tmp_path, monkeypatch)
    publishes = []
    sleeps = []
    monkeypatch.setattr(worker, "_publish_snapshot", lambda: publishes.append(1))

    async def fake_sleep(value):
        sleeps.append(value)
        if len(sleeps) == 3:
            raise TimeoutError("stop the local snapshot loop")

    class Socket:
        def __init__(self):
            self.messages = []
        async def send(self, raw):
            self.messages.append(json.loads(raw))

    monkeypatch.setattr(agent.asyncio, "sleep", fake_sleep)
    sock = Socket()
    with pytest.raises(TimeoutError):
        asyncio.run(worker.heartbeat(sock))
    assert sleeps == [1, 1, 1]
    assert len(publishes) == 3
    assert len(sock.messages) == 1
    assert sock.messages[0]["type"] == "telemetry"


def test_reclaimable_clean_mmap_cache_includes_active_pages(tmp_path):
    # A just-unloaded 2B GGUF often remains in active_file cache, which
    # Linux can reclaim on pressure. Inactive-only gave false 4B failures.
    stat = tmp_path / "memory.stat"
    gib = 1024 ** 3
    stat.write_text(
        "file " + str(3 * gib) + "\n"
        "inactive_file " + str(gib // 4) + "\n"
        "active_file " + str(2 * gib) + "\n"
        "shmem " + str(gib // 2) + "\n"
        "file_dirty " + str(gib // 4) + "\n"
        "file_writeback 0\n"
    )
    assert agent._reclaimable_file_cache(str(stat), "inactive_file") == (
        3 * gib - gib // 2 - gib // 4
    )


def test_reclaimable_file_cache_never_counts_dirty_or_tmpfs(tmp_path):
    stat = tmp_path / "memory.stat"
    stat.write_text("file 1024\nshmem 600\nfile_dirty 300\nfile_writeback 100\ninactive_file 100\n")
    assert agent._reclaimable_file_cache(str(stat), "inactive_file") == 24
    stat.write_text("inactive_file 123\n")
    assert agent._reclaimable_file_cache(str(stat), "inactive_file") == 123



def test_worker_accepts_only_checksum_pinned_remote_catalog_and_persists(tmp_path, monkeypatch):
    from compute_catalog import BUILTIN_CATALOG
    fake = {"test-model": {"name": "Demo", "repo": "test/repo", "file": "demo.gguf",
                           "revision": "a"*40, "size": 21, "sha256": "b"*64, "ctx":4096}}
    worker = isolated_worker(tmp_path, monkeypatch)
    worker.mark_model("test-model", "supported")
    custom = {"custom-1": {"name": "New GGUF", "repo": "owner/repo",
                           "file": "model.gguf", "revision": "c"*40,
                           "size": 100, "sha256": "d"*64, "ctx":4096}}
    catalog = {**BUILTIN_CATALOG, **custom}

    class Ws:
        async def send(self, raw): pass

    async def run():
        await worker.receive_policy(Ws(), {"type":"policy", "revision":2,
                                           "catalog":catalog,"models":[]})
        assert "custom-1" in agent.CATALOG
        assert "test-model" not in worker.capabilities
        assert (agent.DATA / "approved-model-catalog.json").exists()
        if worker.reconcile_task:
            await asyncio.wait_for(worker.reconcile_task, 5)

        bad = {**catalog, "custom-1": {**custom["custom-1"], "revision": "main"}}
        await worker.receive_policy(Ws(), {"type":"policy", "revision":3,
                                           "catalog":bad,"models":[]})
        assert agent.CATALOG["custom-1"]["revision"] == "c"*40
        assert worker.policy_revision == 2, "Malformed catalog must not update policy"

    asyncio.run(run())

    restored = agent.Worker()
    assert "custom-1" in agent.CATALOG
    assert restored.catalog_path.exists()
    assert restored.catalog_path.read_text().find('"node_secret"') < 0


def test_explicit_model_self_test_reports_supported_or_disk_error(tmp_path, monkeypatch):
    worker = isolated_worker(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "memory_limit", lambda: 8 * 1024**3)
    monkeypatch.setattr(agent, "memory_available", lambda: 8 * 1024**3)

    class Ws:
        def __init__(self): self.messages = []
        async def send(self, raw): self.messages.append(json.loads(raw))

    async def fake_unload():
        worker.active = worker.process = None

    async def fake_install(mid):
        return []

    async def fake_load(mid):
        worker.active = mid
        worker.process = type("Proc", (), {"returncode": None})()

    async def fake_benchmark(mid):
        return {"generation_tps": 13.0}

    monkeypatch.setattr(worker, "unload", fake_unload)
    monkeypatch.setattr(worker, "ensure_installed", fake_install)
    monkeypatch.setattr(worker, "load", fake_load)
    monkeypatch.setattr(worker, "benchmark", fake_benchmark)

    async def run():
        ws = Ws()
        await worker.command(ws, {"id":"test-good","model":"test-model","action":"test"})
        assert ws.messages[0]["type"] == "result", ws.messages
        assert worker.capabilities["test-model"]["status"] == "supported"
        assert worker.capabilities["test-model"]["benchmark"]["generation_tps"] == 13.0
        monkeypatch.setattr(agent, "DISK_LIMIT_BYTES", 1)
        await worker.command(ws, {"id":"test-small-disk","model":"test-model","action":"test"})
        assert any(m.get("type")=="error" and "disk" in m.get("error", "").lower()
                   for m in ws.messages), ws.messages
        assert worker.capabilities["test-model"]["status"] == "unsupported"
    asyncio.run(run())
