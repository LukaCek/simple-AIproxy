"""Safe production release acceptance: consistent backups and rollback injection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server_deployment"))
from backup_release import backup_release, checked_database


IMAGE = "sha256:" + "1" * 64


def make_production(tmp_path: Path) -> Path:
    root = tmp_path / "production"
    root.mkdir()
    (root / ".env").write_text("ADMIN_PASSWORD=nonprod-test-secret\n")
    (root / "config.yml").write_text("providers: []\ngroups: {}\n")
    (root / "docker-compose.yml").write_text(
        "services:\n  llm-proxy:\n    image: ghcr.io/lukacek/simple-aiproxy:latest\n"
    )
    with sqlite3.connect(root / "app.db") as db:
        db.execute("CREATE TABLE important (message TEXT)")
        db.execute("INSERT INTO important VALUES ('keep-this-live-record')")
        db.commit()
    return root


def test_release_snapshot_is_transaction_consistent_and_private(tmp_path):
    root = make_production(tmp_path)
    connection = sqlite3.connect(root / "app.db")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO important VALUES ('committed-WAL-record')")
        connection.commit()
        snapshot = backup_release(root, IMAGE)
        checked_database(snapshot / "app.db")
        with sqlite3.connect(snapshot / "app.db") as db:
            rows = db.execute("SELECT message FROM important ORDER BY rowid").fetchall()
        assert rows == [("keep-this-live-record",), ("committed-WAL-record",)]
        assert json.loads((snapshot / "metadata.json").read_text())["previous_image_id"] == IMAGE
        for secret in (snapshot, snapshot / ".env", snapshot / "config.yml",
                       snapshot / "app.db", snapshot / "metadata.json"):
            assert stat.S_IMODE(secret.stat().st_mode) & 0o077 == 0, secret
    finally:
        connection.close()


def test_corrupt_production_database_aborts_snapshot_without_partial_backup(tmp_path):
    root = make_production(tmp_path)
    (root / "app.db").write_bytes(b"not a sqlite database")
    try:
        backup_release(root, IMAGE)
    except (sqlite3.DatabaseError, RuntimeError):
        pass
    else:
        raise AssertionError("Corrupt production DB should fail release")
    assert not list((root / ".release-backups").iterdir())


def test_release_failure_rolls_back_immutable_image_without_overwriting_live_db(tmp_path):
    root = make_production(tmp_path)
    for name in ("update.sh", "backup_release.py", "check_release.py"):
        shutil.copyfile(ROOT / "server_deployment" / name, root / name)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    docker = fakebin / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['FAKE_DOCKER_LOG'], 'a') as f:\n"
        "    f.write(' '.join(args) + '\\n')\n"
        "if args[:1] == ['inspect']:\n"
        "    print('sha256:' + '1'*64)\n"
        "if 'exec' in args and args[-2:] == ['python', '-']:\n"
        "    raise SystemExit(1)  # inject failed post-deploy gate\n"
    )
    docker.chmod(0o700)
    event_log = tmp_path / "docker-events.txt"
    env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ["PATH"],
               FAKE_DOCKER_LOG=str(event_log))
    run = subprocess.run(["bash", "update.sh"], cwd=root, env=env,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode != 0, run.stdout
    events = event_log.read_text()
    assert "pull llm-proxy" in events
    assert "--force-recreate llm-proxy" in events
    assert "simpleai-rollback." in events, events  # rollback override was used
    assert "image tag " in events
    assert "image prune" not in events
    assert "down -v" not in events
    snapshots = list((root / ".release-backups").iterdir())
    assert len(snapshots) == 1
    assert (snapshots[0] / "app.db").exists()
    with sqlite3.connect(root / "app.db") as db:
        assert db.execute("SELECT COUNT(*) FROM important").fetchone()[0] == 1


def test_release_scripts_do_not_restore_db_automatically():
    script = (ROOT / "server_deployment/update.sh").read_text()
    assert "backup_release.py" in script and "check_release.py" in script
    assert 'docker image tag "$previous_image"' in script
    assert "docker image prune" not in script
    assert "down -v" not in script
    assert "cp app.db" not in script
    assert "rm -rf" not in script
    assert "trap 'rollback" in script
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    assert "server_deployment/backup_release.py" in workflow
    assert "server_deployment/check_release.py" in workflow
