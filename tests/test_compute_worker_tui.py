"""Read-only TUI tests: no secrets, live state, resources and model location."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import time

from rich.console import Console

from compute_catalog import CATALOG
from compute_worker_snapshot import (
    STATUS_FILE, write_snapshot, read_snapshot, cgroup_memory_used,
    cgroup_cpu_usage, cpu_percent,
)
from worker.tui import Monitor, human_bytes, progress_bar, sparkline


def fake_snapshot(now):
    return {
        "schema": 1, "updated_at": now, "worker_name": "arm-worker",
        "connected": True, "phase": "generating",
        "phase_started_at": now - 8, "job_started_at": now - 12,
        "current_model": "qwen35-2b-q4", "current_kind": "inference",
        "policy": ["qwen35-2b-q4", "qwen35-0.8b-q4"],
        "hardware": {"cpus": 2, "memory_limit_bytes": 4 * 1024**3},
        "models": {
            "installed": ["qwen35-2b-q4"],
            "active": "qwen35-2b-q4", "ready": True, "busy": True,
            "running_tasks": 1, "policy_revision": 2,
            "disk_used_bytes": 1024**3, "disk_limit_bytes": 8 * 1024**3,
            "capabilities": {"qwen35-2b-q4": {
                "status": "supported", "benchmark": {"generation_tps": 9.8}}}
        }
    }


def test_atomic_snapshot_permissions_and_corruption(tmp_path):
    target = tmp_path / STATUS_FILE
    snapshot = fake_snapshot(time.time())
    write_snapshot(target, snapshot)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert read_snapshot(target) == snapshot
    write_snapshot(target, {**snapshot, "phase": "idle"})
    assert read_snapshot(target)["phase"] == "idle"
    target.write_text("{broken JSON")
    assert read_snapshot(target) is None
    target.write_text("a" * (128 * 1024 + 2))
    assert read_snapshot(target) is None


def test_linux_cgroup_counters(tmp_path):
    (tmp_path / "memory.current").write_text("12345678\n")
    (tmp_path / "cpu.stat").write_text("nr_periods 12\nusage_usec 800000\n")
    assert cgroup_memory_used(tmp_path) == 12345678
    assert cgroup_cpu_usage(tmp_path) == 800000
    assert cpu_percent(None, (800000, 10.0), 2) is None
    assert round(cpu_percent((800000, 10), (1800000, 11), 2), 2) == 50.0
    assert cpu_percent((5, 10), (4, 11), 1) is None


def test_render_shows_running_ram_disk_available_and_speed(tmp_path):
    data = tmp_path / "data"
    models = tmp_path / "models"
    cgroup = tmp_path / "cgroup"
    data.mkdir()
    models.mkdir()
    cgroup.mkdir()
    now = 1760000000
    write_snapshot(data / STATUS_FILE, fake_snapshot(now))
    (cgroup / "memory.current").write_text(str(1024**3))
    (cgroup / "cpu.stat").write_text("usage_usec 1000000\n")
    monitor = Monitor(data=data, models=models, cgroup=cgroup)
    first = monitor.collect(now=now, monotonic=10)
    second = monitor.collect(now=now + 1, monotonic=11)
    console = Console(record=True, width=140, color_system=None, force_terminal=False)
    console.print(monitor.render(second, 140))
    content = console.export_text()
    assert "CONNECTED" in content
    assert "RUNNING" in content
    assert "qwen35-2b-q4" in content
    assert "Loaded" in content
    assert "Saved" in content
    assert "9.8 t/s" in content
    assert "50%" not in content or "CPU quota" in content
    assert len(CATALOG) >= 4
    assert all("qwen" in k for k in CATALOG)


def test_stale_or_missing_worker_does_not_claim_running(tmp_path):
    monitor = Monitor(data=tmp_path, models=tmp_path, cgroup=tmp_path)
    absent = monitor.collect(now=1000, monotonic=1)
    console = Console(record=True, width=90, color_system=None)
    console.print(monitor.render(absent, 90))
    assert "NO LIVE DATA" in console.export_text()

    write_snapshot(tmp_path / STATUS_FILE, fake_snapshot(1000))
    stale = monitor.collect(now=1100, monotonic=2)
    assert not stale["fresh"]
    console = Console(record=True, width=90, color_system=None)
    console.print(monitor.render(stale, 90))
    assert "NO LIVE DATA" in console.export_text()


def test_display_helpers_never_invent_resource_values():
    assert human_bytes(None) == "—"
    assert human_bytes(1024**3) == "1.00 GiB"
    assert progress_bar(None, None) == "░" * 14
    assert len(progress_bar(50, 100)) == 14


def test_btop_keyboard_refresh_controls():
    from worker.tui import change_interval
    assert change_interval(1.0, "+") == 0.5
    assert change_interval(1.0, "-") == 2.0
    assert change_interval(0.25, "+") == 0.25
    assert change_interval(8.0, "-") == 8.0
    assert change_interval(2.0, "r") == 2.0
