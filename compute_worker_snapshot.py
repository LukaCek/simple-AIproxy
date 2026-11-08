"""Read-only, atomic worker status snapshots for local terminal observability.

Snapshots contain *no* prompts, API keys, enrollment tokens or node secrets.
They live in the existing /data volume and are never sent over the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATUS_FILE = "worker-runtime-status.json"
MAX_STATUS_BYTES = 128 * 1024


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """Replace the status file atomically, writable by the worker user only."""
    data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_STATUS_BYTES:
        raise ValueError("Worker status exceeds size limit")
    path = Path(path)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_snapshot(path: Path) -> dict[str, Any] | None:
    """A missing, corrupt or oversized snapshot is an offline/waiting state."""
    try:
        path = Path(path)
        if path.stat().st_size > MAX_STATUS_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("schema") == 1 else None


def cgroup_memory_used(base: Path = Path("/sys/fs/cgroup")) -> int | None:
    """Memory charged to container incl. page cache (not host-wide usage)."""
    for name in ("memory.current", "memory/memory.usage_in_bytes"):
        try:
            amount = int((base / name).read_text().strip())
            if amount >= 0:
                return amount
        except (OSError, ValueError):
            continue
    return None


def cgroup_cpu_usage(base: Path = Path("/sys/fs/cgroup")) -> int | None:
    """CPU time of container in microseconds, independent of host CPU load."""
    try:
        for line in (base / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        return int((base / "cpuacct/cpuacct.usage").read_text().strip()) // 1000
    except (OSError, ValueError):
        return None


def cpu_percent(previous: tuple[int, float] | None, current: tuple[int, float],
                cpus: float) -> float | None:
    """Quota-normalized CPU utilization, avoiding fake numbers on first sample."""
    if previous is None or cpus <= 0 or current[1] <= previous[1] or current[0] < previous[0]:
        return None
    delta_us = current[0] - previous[0]
    wall_us = (current[1] - previous[1]) * 1_000_000
    return min(100.0, max(0.0, 100 * delta_us / (wall_us * cpus)))


def llama_server_process(proc_root: Path = Path("/proc")) -> dict[str, int] | None:
    """Read CPU time and resident memory of llama-server, excluding the TUI.

    Linux /proc/PID/stat reports CPU time for the entire process, including
    llama.cpp inference threads. This is a read-only best-effort measurement.
    It does not estimate model-loading percentage (mmap can be lazy).
    """
    try:
        pids = (item for item in proc_root.iterdir() if item.name.isdigit())
        for pid_path in pids:
            try:
                if (pid_path / "comm").read_text().strip() != "llama-server":
                    continue
                raw = (pid_path / "stat").read_text()
                # comm is parenthesized and may contain spaces and parentheses.
                close = raw.rfind(")")
                if close < 0:
                    continue
                fields = raw[close + 1:].split()
                # fields[0] is /proc/stat field 3 (state)
                ticks = int(fields[11]) + int(fields[12])  # utime + stime
                pages = int(fields[21])  # resident set size (pages)
                clock = os.sysconf("SC_CLK_TCK")
                page_size = os.sysconf("SC_PAGE_SIZE")
                return {"pid": int(pid_path.name),
                        "cpu_usec": max(0, int(ticks * 1_000_000 / clock)),
                        "rss_bytes": max(0, pages * page_size)}
            except (OSError, ValueError, IndexError, ZeroDivisionError):
                continue
    except OSError:
        pass
    return None
