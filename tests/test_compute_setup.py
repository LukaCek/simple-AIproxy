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
    assert service["oom_score_adj"] == 500
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


def test_install_script_supports_single_copy_paste_and_legacy_hidden_prompt():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "worker" / "install.sh").read_text()
    assert "[[ $# -eq 1 || $# -eq 2 ]]" in script
    assert 'ENROLLMENT_CODE="$2"' in script
    assert "read -r -s" in script
    assert "</dev/tty" in script
    assert "docker compose" in script
    assert "docker.sock" not in script
    assert "/compute/bootstrap" in script
    assert "WORKER_ENROLLMENT_TOKEN=" in script
    assert 'info "$ENROLLMENT_CODE"' not in script


def test_wizard_defaults_auto_max_and_hides_manual_fields():
    from pathlib import Path
    wizard = (Path(__file__).resolve().parents[1] /
              "templates" / "compute_setup.html").read_text()
    assert '<option value="auto_max" selected>' in wizard
    assert '<div id="manual-fields" style="display:none">' in wizard
    assert '"manual-fields").style.display = manual ? "contents" : "none"' in wizard
    assert '$(id).disabled = !manual;' in wizard
    assert '$(id).required = manual;' in wizard
    assert 'quote(sourceOrigin) + " " + quote(data.install_code)' in wizard
    assert 'id="copy-code"' not in wizard


def test_local_worker_ca_setup_passed_as_one_command_argument():
    settings = validate_setup({**VALID, "name": "oblak-vps-1"})
    settings["local_test"] = True
    worker = yaml.safe_load(compose_text(settings))["services"]["compute-worker"]
    assert worker["entrypoint"] == ["/bin/sh", "-ec"]
    assert isinstance(worker["command"], list)
    assert len(worker["command"]) == 1
    assert "cat /etc/ssl/certs/ca-certificates.crt" in worker["command"][0]
    assert "exec python agent.py" in worker["command"][0]


def test_auto_memory_budget_uses_total_ram_not_one_time_free_ram():
    """Exercise the actual installer Python calculator, not a copied formula."""
    import ast
    import math
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "worker" / "install.sh").read_text()
    marker = 'INSTALL_DIR="$(python3 - "$TEMP_DIR/bootstrap.json" "$PROXY_ORIGIN" <<\'PY\'\n'
    embedded = script.split(marker, 1)[1].split("\nPY\n", 1)[0]
    tree = ast.parse(embedded)
    calculator = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == "auto_ram_gib")
    gib = 1024 ** 3
    scope = {"math": math, "gib": gib}
    exec(compile(ast.fix_missing_locations(
        ast.Module(body=[calculator], type_ignores=[])), "<auto-installer>", "exec"), scope)
    calculate = scope["auto_ram_gib"]

    # Same 23 GiB machine: active miner (low MemAvailable) cannot
    # freeze the permanent worker Docker limit to 7.25 GiB.
    assert calculate(23 * gib, False) == 19.5
    assert calculate(23 * gib, True) == 12.5
    assert calculate(4 * gib, False) == 2.0
    assert calculate(2 * gib, False) < 2  # small hosts fail safely

    # The runtime guard remains: no unlimited Docker memory and no
    # inference when current host/cgroup memory is insufficient.
    assert "available - " not in embedded
    assert "available = read_mem('MemAvailable')" in embedded
    assert "WORKER_MEMORY_LIMIT={memory_gb}g" in embedded
    assert "if memory_gb < 2:" in embedded


def test_wizard_explains_physical_host_ram_budget_and_heavy_loads():
    from pathlib import Path
    wizard = (Path(__file__).resolve().parents[1] /
              "templates" / "compute_setup.html").read_text()
    assert "85% of total physical RAM" in wizard
    assert "Running other memory-heavy services" in wizard
    assert "Auto Shared" in wizard
