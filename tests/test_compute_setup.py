"""Setup wizard is an admin-only generator, never a Docker execution endpoint."""
import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient

from compute_setup import validate_setup, compose_text, env_text

VALID = {"name": "pi-worker", "proxy_url": "https://proxy.example.com",
         "memory_gb": 4, "disk_gb": 8, "cpus": 2, "threads": 2}


def test_generated_compose_is_valid_and_has_no_inline_secret():
    config = validate_setup(VALID)
    compose = compose_text(config)
    data = yaml.safe_load(compose)
    service = data["services"]["compute-worker"]
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["mem_limit"] == "4g"
    assert service["memswap_limit"] == "4g"
    assert service["environment"]["WORKER_NAME"] == "pi-worker"
    assert "PRIVATE_TOKEN" not in compose
    assert "WORKER_ENROLLMENT_TOKEN" in compose
    assert "WORKER_ENROLLMENT_TOKEN=PRIVATE_TOKEN" in env_text(config, "PRIVATE_TOKEN" * 4)


@pytest.mark.parametrize("invalid", [
    {"name": "bad\nname"},
    {"name": "bad: name"},
    {"proxy_url": "http://example.com"},
    {"proxy_url": "https://user:secret@example.com"},
    {"proxy_url": "https://example.com/foo"},
    {"proxy_url": "https://example.com/\n"},
    {"memory_gb": 0}, {"disk_gb": -2},
    {"cpus": True}, {"cpus": float("nan")}, {"threads": 100},
])
def test_rejects_unsafe_or_impossible_setup(invalid):
    with pytest.raises(ValueError):
        validate_setup({**VALID, **invalid})


def test_setup_endpoint_issues_no_store_short_lived_enrollment(tmp_path, monkeypatch):
    import main_impl as core
    import compute_plane
    compute_plane.install(core.app)
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "wizard.db")
    monkeypatch.setattr(core, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(core, "ADMIN_PASSWORD", "admin")
    core.init_database()
    with TestClient(core.app) as client:
        path = "/admin/api/compute/setup"
        assert client.post(path, json=VALID).status_code == 401
        assert client.post(path, json=VALID, auth=("admin", "admin")).status_code == 403
        response = client.post(path, json=VALID, auth=("admin", "admin"),
                      headers={"X-SimpleAI-Admin-Action": "1"})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert "WORKER_ENROLLMENT_TOKEN=" in data["env"]
        assert "WORKER_ENROLLMENT_TOKEN=" not in data["compose"]
        assert data["expires_at"] > 0
        assert client.get("/admin/compute-setup", auth=("admin", "admin")).status_code == 200
