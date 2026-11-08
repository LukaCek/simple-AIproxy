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
        updater = client.get("/compute/auto-update.sh")
        assert updater.status_code == 200
        assert "# SimpleAI Managed Worker Updater v1" in updater.text
        assert updater.headers["cache-control"] == "no-store"

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


def test_one_word_tui_shortcut_works_from_any_directory(tmp_path, monkeypatch):
    """Check generated launcher using a stub Docker command, not host Docker."""
    import os
    import subprocess
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "worker" / "install.sh").read_text()
    launcher = script.split("cat > \"$tmp\" <<'SIMPLEAI_TUI_LAUNCHER'\n", 1)[1].split(
        "\nSIMPLEAI_TUI_LAUNCHER\n", 1)[0]
    assert "WORKER_ENROLLMENT_TOKEN" not in launcher
    assert "node_secret" not in launcher

    home = tmp_path / "home"
    worker_root = home / ".local/share/simpleai-compute"
    worker_root.mkdir(parents=True)
    first = worker_root / "first-node"
    first.mkdir()
    (first / "docker-compose.yml").write_text("services:\n  compute-worker: {}\n")
    launcher_file = tmp_path / "simpleai-tui"
    launcher_file.write_text(launcher)
    launcher_file.chmod(0o700)

    mock_dir = tmp_path / "mock-bin"
    mock_dir.mkdir()
    mock_docker = mock_dir / "docker"
    mock_docker.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$*\" > \"$SIMPLEAI_TUI_TEST_LOG\"\n"
    )
    mock_docker.chmod(0o700)
    log = tmp_path / "docker.log"
    env = {**os.environ, "HOME": str(home),
           "PATH": str(mock_dir) + os.pathsep + os.environ.get("PATH", ""),
           "SIMPLEAI_TUI_TEST_LOG": str(log)}
    assert subprocess.run(["bash", "-n", str(launcher_file)], check=False).returncode == 0

    # One installed worker: no name or cd needed.
    single = subprocess.run([str(launcher_file), "--once"], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert single.returncode == 0, single.stderr
    assert log.read_text().splitlines() == [
        str(first), "compose exec -T compute-worker python tui.py --once"
    ]

    # Multiple installed workers: require the explicit name, never guess.
    second = worker_root / "second-node"
    second.mkdir()
    (second / "docker-compose.yml").write_text("services:\n  compute-worker: {}\n")
    ambiguous = subprocess.run([str(launcher_file), "--once"], cwd=tmp_path,
                               env=env, capture_output=True, text=True)
    assert ambiguous.returncode == 2
    assert "simpleai-tui first-node" in ambiguous.stderr
    assert "simpleai-tui second-node" in ambiguous.stderr

    specific = subprocess.run([str(launcher_file), "second-node", "--once"],
                              cwd=tmp_path, env=env, capture_output=True, text=True)
    assert specific.returncode == 0, specific.stderr
    assert log.read_text().splitlines()[0] == str(second)

    path_traversal = subprocess.run([str(launcher_file), "../other", "--once"],
                                    cwd=tmp_path, env=env, capture_output=True, text=True)
    assert path_traversal.returncode != 0
    assert "Invalid worker name" in path_traversal.stderr


def test_installer_auto_opens_live_tui_and_installs_private_shortcut():
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "worker" / "install.sh").read_text()
    assert 'local launcher="$bin_dir/simpleai-tui"' in script
    assert 'chmod 700 "$tmp"' in script
    assert 'mv -f -- "$tmp" "$launcher"' in script
    assert '"$HOME/.local/bin/simpleai-tui" "$(basename "$INSTALL_DIR")" </dev/tty >/dev/tty 2>&1' in script
    assert 'docker compose exec compute-worker python tui.py' in script
    assert 'docker compose exec -T compute-worker python tui.py --once' in script


def test_host_auto_updater_stays_outside_worker_and_is_syntax_valid(tmp_path):
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    script = repo / "worker" / "auto-update.sh"
    result = subprocess.run(["bash", "-n", str(script)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    content = script.read_text()
    installer = (repo / "worker" / "install.sh").read_text()
    assert "flock -n 9" in content
    assert "docker pull --quiet" in content
    assert 'docker image tag "$old" "$image"' in content
    assert "wait_connected" in content
    assert "worker_idle" in content
    assert "Persistent=true" in content
    assert "systemctl --user enable --now" in content
    assert "sudo loginctl enable-linger" in content
    assert "/var/run/docker.sock" not in installer
    assert 'bash -s -- --install "$(basename "$INSTALL_DIR")" "$PROXY_ORIGIN"' in installer

    # The updater cannot auto-pull arbitrary images or modify an unrelated
    # Compose project; docker compose config is validated.
    assert 'actual=="ghcr.io/lukacek/simple-aiproxy-worker:latest"' in content


def test_auto_updater_rejects_invalid_worker_name_without_docker(tmp_path):
    import subprocess
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] /
              "worker" / "auto-update.sh")
    for name in ("../other", "bad/name", "bad name", "-force"):
        output = subprocess.run(["bash", str(script), "--check", name],
                                capture_output=True, text=True)
        assert output.returncode != 0
        assert "valid worker name" in output.stderr


def test_host_updater_only_recreates_worker_for_new_image_when_idle(tmp_path):
    """Stub Docker API to exercise no-op, busy and upgrade paths safely."""
    import os
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "worker" / "auto-update.sh"
    root = tmp_path / "home" / ".local/share/simpleai-compute" / "compute-1"
    root.mkdir(parents=True)
    (root / "docker-compose.yml").write_text(
        "services:\n  compute-worker:\n    image: ghcr.io/lukacek/simple-aiproxy-worker:latest\n")
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    docker = mock_bin / "docker"
    docker.write_text(r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ["MOCK_DOCKER_LOG"], "a") as f:
    f.write(" ".join(a) + "\n")
if a[:2] == ["compose", "config"]:
    print(json.dumps({"services": {"compute-worker": {
        "image": "ghcr.io/lukacek/simple-aiproxy-worker:latest"}}}))
elif a[:3] == ["compose", "ps", "-q"]:
    print("container-1")
elif a[:2] == ["inspect", "--format"]:
    print("running" if "State.Status" in a[2] else "sha256:old")
elif a[:3] == ["compose", "exec", "-T"]:
    if os.environ.get("MOCK_BUSY") == "1":
        sys.exit(1)
    # Only reject the newly updated container's connectivity check.
    if os.environ.get("MOCK_REJECT") == "1" and "assert s.get(\"connected\")" in " ".join(a):
        text = open(os.environ["MOCK_DOCKER_LOG"]).read()
        if "image tag sha256:old ghcr.io/lukacek/simple-aiproxy-worker:latest" not in text:
            sys.exit(1)
elif a[:3] == ["image", "inspect", "--format"]:
    print("sha256:" + os.environ.get("MOCK_RELEASE", "old"))
''')
    docker.chmod(0o700)

    log = tmp_path / "mock-docker.log"
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "PATH": str(mock_bin) + os.pathsep + os.environ.get("PATH", ""),
           "MOCK_DOCKER_LOG": str(log)}
    def run(**kwargs):
        log.write_text("")
        return subprocess.run(["bash", str(script), "--check", "compute-1"],
                              env={**env, **kwargs}, capture_output=True,
                              text=True, timeout=20)

    current = run(MOCK_RELEASE="old")
    assert current.returncode == 0, current.stderr
    assert "Already current" in current.stdout
    assert "compose up " not in log.read_text()

    busy = run(MOCK_RELEASE="new", MOCK_BUSY="1")
    assert busy.returncode == 0, busy.stderr
    assert "defer update" in busy.stdout
    assert "pull --quiet" not in log.read_text()

    upgraded = run(MOCK_RELEASE="new")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "Updated successfully" in upgraded.stdout
    assert "compose up -d --no-build --no-deps --force-recreate" in log.read_text()
    assert not (root / ".update-rejected-image").exists()

    # A failed image triggers rollback; the exact failed image ID is blocked
    # until a different upstream release arrives.
    sleeper = mock_bin / "sleep"
    sleeper.write_text("#!/bin/sh\nexit 0\n")
    sleeper.chmod(0o700)
    failed = run(MOCK_RELEASE="new", MOCK_REJECT="1")
    assert failed.returncode == 0, failed.stderr
    assert "Rollback successful" in failed.stdout
    assert (root / ".update-rejected-image").read_text().strip() == "sha256:new"
    assert "image tag sha256:old ghcr.io/lukacek/simple-aiproxy-worker:latest" in log.read_text()
    blocked = run(MOCK_RELEASE="new")
    assert blocked.returncode == 0, blocked.stderr
    assert "Previously failed image" in blocked.stdout
    assert "compose up " not in log.read_text()


def test_auto_updater_install_enables_persistent_user_timer_without_real_docker(tmp_path):
    """One-time adoption works for existing identity/model volumes."""
    import os
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "worker" / "auto-update.sh"
    home = tmp_path / "home"
    root = home / ".local/share/simpleai-compute/demo"
    root.mkdir(parents=True)
    (root / "docker-compose.yml").write_text(
        "services:\n  compute-worker:\n    image: ghcr.io/lukacek/simple-aiproxy-worker:latest\n")
    (root / ".env").write_text("SIMPLEAI_PROXY_URL=https://proxy.example.com\n")
    (root / "models.marker").write_text("KEEP MODEL STATE")
    commands = tmp_path / "bin"
    commands.mkdir()
    actions = tmp_path / "actions.log"

    docker = commands / "docker"
    docker.write_text(r'''#!/usr/bin/env python3
import json, os, sys
a=sys.argv[1:]
if a[:2]==["compose","config"]:
    print(json.dumps({"services":{"compute-worker":{"image":
      "ghcr.io/lukacek/simple-aiproxy-worker:latest"}}}))
sys.exit(0)
''')
    docker.chmod(0o700)

    curl = commands / "curl"
    curl.write_text(r'''#!/usr/bin/env python3
import os, shutil, sys
args=sys.argv[1:]
shutil.copyfile(os.environ["MOCK_UPDATE_SOURCE"], args[args.index("-o")+1])
''')
    curl.chmod(0o700)

    systemctl = commands / "systemctl"
    systemctl.write_text(r'''#!/usr/bin/env python3
import os, sys
with open(os.environ["MOCK_ACTIONS"], "a") as f:
    f.write("systemctl " + " ".join(sys.argv[1:]) + "\n")
''')
    systemctl.chmod(0o700)

    loginctl = commands / "loginctl"
    loginctl.write_text("#!/bin/sh\nprintf 'yes\\n'\n")
    loginctl.chmod(0o700)

    sudo = commands / "sudo"
    sudo.write_text("#!/bin/sh\nexit 19\n")
    sudo.chmod(0o700)

    env = {**os.environ,
           "HOME": str(home),
           "PATH": str(commands) + os.pathsep + os.environ.get("PATH", ""),
           "MOCK_UPDATE_SOURCE": str(script),
           "MOCK_ACTIONS": str(actions)}
    completed = subprocess.run(
        ["bash", str(script), "--install", "demo", "https://proxy.example.com"],
        env=env, capture_output=True, text=True, timeout=25)
    assert completed.returncode == 0, completed.stderr
    assert "Automatic updates enabled" in completed.stdout
    assert (root / "models.marker").read_text() == "KEEP MODEL STATE"
    copied = root / "auto-update.sh"
    assert copied.read_bytes() == script.read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o700
    units = home / ".config/systemd/user"
    service = (units / "simpleai-compute-update@.service").read_text()
    timer = (units / "simpleai-compute-update@.timer").read_text()
    assert "ExecStart=%h/.local/share/simpleai-compute/%i/auto-update.sh" in service
    assert "Persistent=true" in timer
    assert "OnCalendar=" in timer
    assert "enable --now simpleai-compute-update@demo.timer" in actions.read_text()


def test_installer_bootstraps_docker_without_breaking_existing_working_docker(tmp_path):
    """Execute real installer host preflight with a mocked working Docker CLI."""
    import os
    import subprocess
    from pathlib import Path

    original = (Path(__file__).resolve().parents[1] / "worker" / "install.sh").read_text()
    assert "SimpleAI Compute Worker — isolated Linux installation" in original
    prefix = original.split('info "SimpleAI Compute Worker — isolated Linux installation"', 1)[0]
    script = tmp_path / "preflight.sh"
    script.write_text(prefix + '\nprintf "BOOTSTRAP_PASSED\\n"\n')
    mock = tmp_path / "bin"
    mock.mkdir()
    log = tmp_path / "root-changes.log"
    docker = mock / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'compose version'|'info ' ) exit 0 ;;\n"
        "  * ) exit 99 ;;\n"
        "esac\n"
    )
    docker.chmod(0o755)
    for tool in ("sudo", "systemctl", "pacman", "apt-get", "dnf", "usermod", "sg"):
        stub = mock / tool
        stub.write_text("#!/bin/sh\nprintf '%s\\n' '" + tool +
                        "' >> \"$SIMPLEAI_TEST_CHANGES\"\nexit 99\n")
        stub.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path),
           "PATH": str(mock) + os.pathsep + os.environ.get("PATH", ""),
           "SIMPLEAI_TEST_CHANGES": str(log)}
    finished = subprocess.run(["bash", str(script), "https://proxy.example.com"],
                              env=env, capture_output=True, text=True, timeout=15)
    assert finished.returncode == 0, finished.stderr
    assert "BOOTSTRAP_PASSED" in finished.stdout
    assert not log.exists(), "Existing working Docker must never trigger sudo/pacman/systemctl"


def test_installer_host_bootstrap_has_explicit_security_boundaries():
    """Check fail-closed defaults for privileged operations and distro support."""
    from pathlib import Path
    installer = (Path(__file__).resolve().parents[1] / "worker" / "install.sh").read_text()

    assert 'install_host_packages\nstart_host_docker\nensure_docker_user_access' in installer
    assert installer.index("install_host_packages\nstart_host_docker") < installer.index(
        "ENROLLMENT_CODE=\"$2\""), "Never claim a short-lived code before host prerequisites"
    assert "pacman -S --needed --noconfirm" in installer
    assert "No -Sy: never risk a partial upgrade" in installer
    assert "apt-get update" in installer and "apt-get install -y" in installer
    assert "dnf install -y" in installer
    assert "systemctl enable --now docker.service" in installer
    assert "root-equivalent privileges" in installer
    assert "Type YES" in installer
    assert "usermod -aG docker" in installer
    assert 'exec sg docker -c "$cmd"' in installer
    assert 'SIMPLEAI_DOCKER_GROUP_REENTRY' in installer
    assert "sudo -v </dev/tty" in installer
    assert "DOCKER_HOST is set" in installer
    assert "$base.sha256" in installer
    assert '[[ "$expected" =~ ^[a-fA-F0-9]{64}$ && "$expected" == "$actual" ]]' in installer
    assert 'Docker daemon is unavailable. Start Docker or configure access' not in installer
