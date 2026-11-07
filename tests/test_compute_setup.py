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


def test_auto_profiles_require_host_measurements_and_keep_docker_unprivileged():
    for mode in ("auto_shared", "auto_max"):
        settings = validate_setup({**VALID, "profile": mode})
        compose = compose_text(settings)
        data = yaml.safe_load(compose)
        worker = data["services"]["compute-worker"]
        assert "WORKER_MEMORY_LIMIT" in worker["mem_limit"]
        assert "WORKER_CPU_LIMIT" in worker["cpus"]
        assert worker["cap_drop"] == ["ALL"]
        assert worker["read_only"] is True
        assert "/var/run/docker.sock" not in compose
    with pytest.raises(ValueError):
        validate_setup({**VALID, "profile": "remote_exec"})


def test_home_test_compose_is_isolated_and_uses_existing_public_ca():
    settings = validate_setup({**VALID, "name": "oblak-vps-1"})
    settings["local_test"] = True
    data = yaml.safe_load(compose_text(settings))
    node = data["services"]["compute-worker"]
    assert node["image"] == "simpleai-home-test/worker:local"
    assert node["mem_limit"] == "4g"
    assert "worker-data:/data" in node["volumes"]
    assert "worker-models:/models" in node["volumes"]
    assert "staging-ca:/staging-ca:ro" in node["volumes"]
    assert data["volumes"]["staging-ca"]["external"] is True
    assert data["volumes"]["staging-ca"]["name"] == "simpleai-home-test_staging-ca"
    assert data["networks"]["default"]["name"] == "simpleai-home-test_default"
    assert "docker.sock" not in compose_text(settings)


def test_installer_is_public_but_bootstrap_requires_unused_matching_token(tmp_path, monkeypatch):
    import main_impl as core
    import compute_plane
    compute_plane.install(core.app)
    monkeypatch.setattr(core, "DB_PATH", tmp_path / "installer.db")
    monkeypatch.setattr(core, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(core, "ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("AIPROXY_COMPUTE_HOME_TEST", "1")
    core.init_database()
    with TestClient(core.app) as client:
        public = client.get("/compute/install.sh")
        assert public.status_code == 200
        assert "SimpleAI Compute Worker" in public.text
        assert "no-store" in public.headers["cache-control"]
        assert client.post("/compute/bootstrap", json={"token": "not-valid"}).status_code == 401
        request = {**VALID, "name": "oblak-vps-1"}
        generated = client.post("/admin/api/compute/setup", json=request,
                                auth=("admin", "admin"),
                                headers={"X-SimpleAI-Admin-Action": "1"})
        assert generated.status_code == 200, generated.text
        definition = generated.json()
        code = definition["install_code"]
        assert len(code) > 32
        response = client.post("/compute/bootstrap", json={"token": code})
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["name"] == "oblak-vps-1"
        assert payload["profile"] == "manual"
        assert payload["local_test"] is True
        assert "simpleai-home-test/worker:local" in payload["compose"]
        assert "WORKER_ENROLLMENT_TOKEN=" + code in payload["env"]
        assert code not in payload["compose"]
        enrolled = client.post("/compute/enroll", json={
            "token": code, "name": "oblak-vps-1"
        })
        assert enrolled.status_code == 200
        assert client.post("/compute/enroll", json={"token": code}).status_code == 401
        assert client.post("/compute/bootstrap", json={"token": code}).status_code == 401
        assert client.post("/compute/bootstrap", json={"token": "a" * 44}).status_code == 401


def test_install_script_is_static_and_avoids_token_in_shell_history():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "worker" / "install.sh").read_text()
    assert "read -r -s" in script
    assert "</dev/tty" in script
    assert "docker compose" in script
    assert "docker.sock" not in script
    assert "/compute/bootstrap" in script
    assert "WORKER_ENROLLMENT_TOKEN=" in script
