"""Dynamic Hugging Face GGUF catalog and safe node lifecycle tests."""
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_zz_compute_plane import fixture_client, enroll_node, ADMIN

WRITE = {"X-SimpleAI-Admin-Action": "1"}
SHA = "a" * 40
HASH = "b" * 64

FAKE_MODEL = {
    "name": "Example 1B Q4", "repo": "example/model-GGUF",
    "file": "example-Q4_K_M.gguf", "revision": SHA, "size": 1234567,
    "sha256": HASH, "ctx": 4096,
}


def test_catalog_entry_validation_blocks_malicious_download_targets():
    from compute_catalog import validate_model_entry
    good = validate_model_entry("example-1b", FAKE_MODEL)
    assert good["file"].endswith(".gguf")
    for change in (
        {"repo": "https://evil.com/a"}, {"repo": "owner/../evil"},
        {"file": "../../secrets.gguf"}, {"file": "model.gguf?token=x"},
        {"file": "x.py"}, {"revision": "main"}, {"sha256": "bad"},
        {"size": -1}, {"ctx": 0}, {"name": "x\nscript"},
    ):
        with pytest.raises(ValueError):
            validate_model_entry("example-1b", {**FAKE_MODEL, **change})
    with pytest.raises(ValueError):
        validate_model_entry("../unsafe", FAKE_MODEL)


def test_huggingface_inspection_stays_on_fixed_host_and_requires_lfs_hash(monkeypatch):
    from compute_hf_catalog import lookup_gguf_models, parse_source

    assert parse_source("https://huggingface.co/example/model-GGUF/resolve/main/example-Q4_K_M.gguf") == (
        "example/model-GGUF", "main", "example-Q4_K_M.gguf")
    for bad in ("http://huggingface.co/o/r", "https://evil.org/o/r",
                "https://huggingface.co/o/r/blob/main/../../x.gguf",
                "https://huggingface.co/o/r?redirect=https://evil.org", "not-a-repo"):
        with pytest.raises(ValueError):
            parse_source(bad)

    class FakeResponse:
        content = b"{}"
        def raise_for_status(self): pass
        def json(self): return {
            "sha": SHA,
            "siblings": [
                {"rfilename": FAKE_MODEL["file"],
                 "lfs": {"size": FAKE_MODEL["size"], "sha256": HASH}},
                {"rfilename": "unsafe.gguf", "size": 1},
                {"rfilename": "run.py", "lfs": {"size": 20, "sha256": HASH}},
            ],
        }

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, url, params=None):
            assert url == "https://huggingface.co/api/models/example/model-GGUF/revision/main"
            assert params == {"blobs": "true"}
            return FakeResponse()

    import compute_hf_catalog
    monkeypatch.setattr(compute_hf_catalog.httpx, "AsyncClient", lambda **k: FakeClient())
    result = asyncio.run(lookup_gguf_models("example/model-GGUF"))
    assert result["revision"] == SHA
    assert result["files"] == [FAKE_MODEL | {"name": "example-Q4_K_M"}]


def test_admin_add_catalog_persists_and_pushes_pinned_metadata(tmp_path, monkeypatch):
    import compute_plane
    from compute_catalog import CATALOG, BUILTIN_CATALOG

    async def lookup(_source):
        return {"repo": FAKE_MODEL["repo"], "revision": SHA, "files": [dict(FAKE_MODEL)]}
    monkeypatch.setattr(compute_plane, "lookup_gguf_models", lookup)
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        bad = client.post("/admin/api/compute/catalog/custom",
                          json={"source": "example/model-GGUF", "file": FAKE_MODEL["file"]})
        assert bad.status_code == 401
        resp = client.post("/admin/api/compute/catalog/custom", auth=ADMIN, headers=WRITE,
                           json={"source": "example/model-GGUF", "file": FAKE_MODEL["file"],
                                 "ctx": 8192, "enable": True})
        assert resp.status_code == 200, resp.text
        mid = resp.json()["id"]
        assert mid.startswith("custom-")
        assert resp.json()["enabled"] is True
        assert CATALOG[mid]["ctx"] == 8192
        duplicate = client.post("/admin/api/compute/catalog/custom", auth=ADMIN, headers=WRITE,
                                json={"source": "example/model-GGUF", "file": FAKE_MODEL["file"]})
        assert duplicate.status_code == 409
        read = client.get("/admin/api/compute/catalog", auth=ADMIN).json()
        assert mid in read["models"]
        assert set(BUILTIN_CATALOG) <= set(read["models"])
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type": "auth", "protocol": 1, "catalog_protocol": 1,
                          **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            policy = ws.receive_json()
            assert mid in policy["models"]
            assert policy["catalog"][mid]["revision"] == SHA
            assert policy["catalog"][mid]["sha256"] == HASH
        compute_plane._setup()
        assert mid in CATALOG
        with sqlite3.connect(str(tmp_path / "proxy.db")) as db:
            assert db.execute("SELECT COUNT(*) FROM ComputeCustomModels").fetchone()[0] == 1
        deleted = client.delete("/admin/api/compute/catalog/custom/" + mid,
                                auth=ADMIN, headers=WRITE)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["cached_weights_preserved"] is True
        assert mid not in client.get("/admin/api/compute/catalog", auth=ADMIN).json()["models"]
        assert mid not in client.get("/admin/api/compute/model-policy", auth=ADMIN).json()["models"]
        assert client.delete("/admin/api/compute/catalog/custom/qwen35-0.8b-q4",
                             auth=ADMIN, headers=WRITE).status_code == 400


def test_remove_offline_node_is_permanent_but_never_touches_remote_container(tmp_path, monkeypatch):
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        node_id = identity["node_id"]
        url = f"/admin/api/compute/nodes/{node_id}"
        assert client.delete(url, auth=ADMIN).status_code == 403
        response = client.delete(url, auth=ADMIN, headers=WRITE)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "removed": True, "node_id": node_id, "remote_container_untouched": True}
        assert not any(n["id"] == node_id for n in client.get(
            "/admin/api/compute/nodes", auth=ADMIN).json()["nodes"])
        assert client.delete(url, auth=ADMIN, headers=WRITE).status_code == 404
        with sqlite3.connect(str(tmp_path / "proxy.db")) as db:
            for table in ("ComputeNodes", "ComputeEvents", "ComputeOperations"):
                assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE " +
                                  ("id" if table == "ComputeNodes" else "node_id") + "=?",
                                  (node_id,)).fetchone()[0] == 0


def test_approved_model_can_be_cpu_tested_on_a_selected_worker(tmp_path, monkeypatch):
    with fixture_client(tmp_path, monkeypatch) as client:
        node_id = enroll_node(client)["node_id"]
        result = client.post(f"/admin/api/compute/nodes/{node_id}/operations",
                             auth=ADMIN, headers=WRITE,
                             json={"action": "test", "model": "qwen35-0.8b-q4"})
        assert result.status_code == 409  # offline, but valid action
        page = client.get("/admin/compute-nodes", auth=ADMIN)
        assert page.status_code == 200
        assert "Find GGUF files" in page.text
        assert "Remove worker permanently" in page.text
        assert "state.reason" in page.text



def test_one_time_update_adoption_helper_is_public_but_does_not_touch_worker_data(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "worker" / "enable-auto-updates.sh"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    content = script.read_text()
    assert "simpleai-tui" in content
    assert "--install" in content
    assert "docker compose config" in content
    assert "docker compose down" not in content
    assert "rm -rf" not in content
    assert "WORKER_ENROLLMENT_TOKEN" not in content
    with fixture_client(tmp_path, monkeypatch) as client:
        public = client.get("/compute/enable-auto-updates.sh")
        assert public.status_code == 200
        assert "SimpleAI Compute legacy adoption helper" in public.text
        assert public.headers["cache-control"] == "no-store"



def test_old_worker_receives_only_legacy_models_during_migration(tmp_path, monkeypatch):
    import compute_plane
    async def lookup(_source):
        return {"repo": FAKE_MODEL["repo"], "revision": SHA, "files": [dict(FAKE_MODEL)]}
    monkeypatch.setattr(compute_plane, "lookup_gguf_models", lookup)
    with fixture_client(tmp_path, monkeypatch) as client:
        identity = enroll_node(client)
        resp = client.post("/admin/api/compute/catalog/custom", auth=ADMIN,
                           headers=WRITE,
                           json={"source": "example/model-GGUF", "file": FAKE_MODEL["file"],
                                 "enable": True})
        assert resp.status_code == 200, resp.text
        with client.websocket_connect("/compute/ws") as ws:
            ws.send_json({"type":"auth", "protocol":1, **identity})
            assert ws.receive_json()["type"] == "auth_ok"
            policy = ws.receive_json()
            assert "catalog" not in policy
            assert resp.json()["id"] not in policy["models"]
