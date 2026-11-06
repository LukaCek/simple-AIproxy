"""Model store quota, eviction, and autonomous policy lifecycle tests."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SIMPLEAI_PROXY_URL", "https://example.test")
pytest.importorskip("huggingface_hub")
pytest.importorskip("websockets.asyncio.client")

import compute_worker_storage as storage
from worker import agent


class Download:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def raise_for_status(self):
        pass
    def iter_bytes(self, chunk_size):
        for offset in range(0, len(self.body), 8):
            yield self.body[offset:offset+8]


class Client:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def stream(self, method, url):
        return Download(self.body)


def catalog_for(content):
    return {mid: {"repo": "safe/repo", "file": mid + ".gguf",
                  "revision": "a" * 40, "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(), "ctx": 4096}
            for mid, data in content.items()}


def test_quota_parsing():
    assert storage.disk_limit_bytes("0.5") == 512 * 1024**2
    for invalid in ("", "0", "-3", "NaN", "infinity", "abc"):
        with pytest.raises(ValueError):
            storage.disk_limit_bytes(invalid)


def test_storage_eviction_and_oversized_download(tmp_path, monkeypatch):
    a, b, c = b"a"*1024, b"b"*1024, b"c"*1024
    monkeypatch.setattr(storage, "CATALOG", catalog_for({"a":a, "b":b, "c":c}))
    monkeypatch.setattr(storage, "hf_hub_url", lambda **kwargs: "https://huggingface.co/safe/resolve/sha")
    current = [a]
    monkeypatch.setattr(storage.httpx, "Client", lambda **kwargs: Client(current[0]))
    monkeypatch.setattr(storage, "MIN_HOST_FREE", 0)
    # Small-model allowance is 16MiB; allow one cached file, not two.
    quota = 16 * storage.MIB + 1250
    assert storage.install_model(tmp_path, "a", quota, {}) == []
    assert (tmp_path / "a.gguf").exists()
    current[0] = b
    # Deterministically evict oldest cached model without losing the new one.
    evicted = storage.install_model(tmp_path, "b", quota, {"a": 1})
    assert evicted == ["a"]
    assert not (tmp_path / "a.gguf").exists()
    assert (tmp_path / "b.gguf").read_bytes() == b
    current[0] = c
    with pytest.raises(RuntimeError, match="no evictable"):
        storage.install_model(tmp_path, "c", quota, {"b": 1}, protected={"b"})
    assert not (tmp_path / "c.gguf").exists()

    # A bad or too-large download cannot publish unverified data.
    current[0] = b"malicious"*300
    with pytest.raises(RuntimeError, match="SHA256 mismatch|Download exceeded"):
        storage.install_model(tmp_path, "c", quota, {"b": 1})
    assert not (tmp_path / "c.gguf").exists()
    assert not (tmp_path / "c.part").exists()


def test_used_bytes_skips_symlinks(tmp_path):
    (tmp_path / "blob").write_bytes(b"12345")
    (tmp_path / "linked").symlink_to(tmp_path / "blob")
    assert storage.used_bytes(tmp_path) == 5


def test_policy_auto_benchmarks_and_on_demand_model_swap(tmp_path, monkeypatch):
    payloads = {"a": b"a"*100, "b": b"b"*100}
    catalog = catalog_for(payloads)
    data = tmp_path / "data"
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(agent, "DATA", data)
    monkeypatch.setattr(agent, "MODELS", models)
    monkeypatch.setattr(agent, "IDENTITY", data / "identity.json")
    monkeypatch.setattr(agent, "CATALOG", catalog)
    monkeypatch.setattr(storage, "CATALOG", catalog)
    monkeypatch.setattr(agent, "DISK_LIMIT_BYTES", storage.GIB)
    monkeypatch.setattr(agent, "memory_limit", lambda: 4 * storage.GIB)
    monkeypatch.setattr(agent, "memory_available", lambda: 4 * storage.GIB)

    class Ws:
        def __init__(self):
            self.sent = []
        async def send(self, raw):
            self.sent.append(json.loads(raw))

    ws = Ws()
    worker = agent.Worker()
    tested = []
    installed = []
    active = []

    async def fake_download(mid):
        installed.append(mid)
        (models / (mid + ".gguf")).write_bytes(payloads[mid])
        return []

    async def fake_load(mid):
        active[:] = [mid]
        worker.active = mid
        worker.process = type("Proc", (), {"returncode": None})()

    async def fake_unload():
        active.clear()
        worker.active = worker.process = None

    async def fake_benchmark(mid):
        tested.append(mid)

    async def fake_stream(ws, cid, mid, payload):
        await worker.send(ws, {"type":"end","id":cid,"status":200})

    monkeypatch.setattr(worker, "ensure_installed", fake_download)
    monkeypatch.setattr(worker, "load", fake_load)
    monkeypatch.setattr(worker, "unload", fake_unload)
    monkeypatch.setattr(worker, "benchmark", fake_benchmark)
    monkeypatch.setattr(worker, "_inference_stream", fake_stream)

    async def run():
        await worker.receive_policy(ws, {"type":"policy", "revision":2, "models":["a","b"]})
        await asyncio.wait_for(worker.reconcile_task, 5)
        assert tested == ["b", "a"], "Primary model must be tested last"
        assert worker.status()["capabilities"]["a"]["status"] == "supported"
        assert worker.status()["capabilities"]["b"]["status"] == "supported"
        assert worker.active == "a"
        await worker.inference(ws, {"id":"request-1", "model":"b",
                                   "payload":{"stream":True,"messages":[]}})
        assert worker.active == "b", "Model must be switched on demand"
        assert any(x["type"]=="end" and x["id"]=="request-1" for x in ws.sent)
        await worker.receive_policy(ws, {"type":"policy", "revision":3, "models":[]})
        await asyncio.wait_for(worker.reconcile_task, 5)
        assert worker.active is None

    asyncio.run(run())


def test_reduced_compose_quota_prunes_cached_models(tmp_path, monkeypatch):
    content = {"a": b"a"*1000, "b": b"b"*1000}
    monkeypatch.setattr(storage, "CATALOG", catalog_for(content))
    (tmp_path / "a.gguf").write_bytes(content["a"])
    (tmp_path / "b.gguf").write_bytes(content["b"])
    evicted = storage.prune_to_limit(tmp_path, 1200, {"a": 1, "b": 9})
    assert evicted == ["a"]
    assert not (tmp_path / "a.gguf").exists()
    assert (tmp_path / "b.gguf").exists()
    assert storage.used_bytes(tmp_path) <= 1200


def test_stale_part_file_is_removed_before_install_quota_check(tmp_path, monkeypatch):
    content = b"safe model"
    monkeypatch.setattr(storage, "CATALOG", catalog_for({"a": content}))
    monkeypatch.setattr(storage, "MIN_HOST_FREE", 0)
    monkeypatch.setattr(storage, "hf_hub_url", lambda **kwargs: "https://huggingface.co/test/resolve/sha")
    monkeypatch.setattr(storage.httpx, "Client", lambda **kwargs: Client(content))
    part = tmp_path / "a.part"
    part.write_bytes(b"x" * (16 * storage.MIB))
    limit = 16 * storage.MIB + 1024
    storage.install_model(tmp_path, "a", limit, {})
    assert (tmp_path / "a.gguf").read_bytes() == content
    assert not part.exists()


def test_interrupted_benchmark_is_marked_error_on_worker_restart(tmp_path, monkeypatch):
    catalog = catalog_for({"test": b"test-model"})
    monkeypatch.setattr(agent, "DATA", tmp_path / "data")
    monkeypatch.setattr(agent, "MODELS", tmp_path / "models")
    monkeypatch.setattr(agent, "CATALOG", catalog)
    monkeypatch.setattr(storage, "CATALOG", catalog)
    monkeypatch.setattr(agent, "DISK_LIMIT_BYTES", storage.GIB)
    (tmp_path / "models").mkdir()
    first = agent.Worker()
    first.mark_model("test", "testing")
    restarted = agent.Worker()
    status = restarted.status()["capabilities"]["test"]
    assert status["status"] == "error"
    assert "stopped" in status["reason"].lower()


def test_corrupted_existing_model_is_redownloaded(tmp_path, monkeypatch):
    content = b"valid GGUF content"
    monkeypatch.setattr(storage, "CATALOG", catalog_for({"a": content}))
    monkeypatch.setattr(storage, "hf_hub_url", lambda **kwargs: "https://huggingface.co/test/resolve/sha")
    monkeypatch.setattr(storage.httpx, "Client", lambda **kwargs: Client(content))
    (tmp_path / "a.gguf").write_bytes(b"corrupted")
    storage.install_model(tmp_path, "a", storage.GIB, {})
    assert (tmp_path / "a.gguf").read_bytes() == content
