"""SimpleAI Compute Worker read-only live terminal dashboard.

Run inside the existing container:
  docker compose exec compute-worker python tui.py
For a static, non-interactive snapshot:
  docker compose exec -T compute-worker python tui.py --once

No network, Docker socket, API credentials or model controls are required.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import os
from pathlib import Path
import select
import sys
import termios
import time
import tty
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from compute_catalog import CATALOG
from compute_worker_snapshot import (
    STATUS_FILE, cgroup_cpu_usage, cgroup_memory_used,
    cpu_percent, read_snapshot, llama_server_process,
)

DATA = Path(os.getenv("WORKER_DATA_DIR", "/data"))
MODELS = Path(os.getenv("WORKER_MODEL_DIR", "/models"))
CGROUP = Path("/sys/fs/cgroup")
SPARK = "▁▂▃▄▅▆▇█"
GIB = 1024 ** 3


def change_interval(current: float, key: str) -> float:
    """btop-like keyboard speed control: + faster, - slower."""
    if key in ("+", "="):
        return max(0.25, round(current / 2, 2))
    if key in ("-", "_"):
        return min(8.0, round(current * 2, 2))
    return current


def human_bytes(value: Any) -> str:
    if not isinstance(value, (float, int)) or isinstance(value, bool) or value < 0:
        return "—"
    if value >= GIB:
        return f"{value / GIB:.2f} GiB"
    if value >= 1024 ** 2:
        return f"{value / 1024 ** 2:.0f} MiB"
    return f"{value / 1024:.0f} KiB"


def progress_bar(used: int | None, limit: int | None, width: int = 14) -> str:
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)) or limit <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, float(used) / limit))
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def sparkline(samples: deque[float | None]) -> str:
    vals = [v for v in samples if v is not None]
    if not vals:
        return "No samples yet"
    # Fixed percentage scale: spikes do not make idle activity look saturated.
    return "".join(SPARK[min(7, max(0, int(v / 100 * 8)))] if v is not None else "·"
                   for v in samples)


def safe_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def elapsed(start: Any, now: float) -> str:
    try:
        seconds = int(max(0, now - float(start)))
        if seconds > 7 * 24 * 3600:
            return "—"
        return f"{seconds // 3600:02d}:{(seconds // 60) % 60:02d}:{seconds % 60:02d}"
    except (TypeError, ValueError, OverflowError):
        return "—"


class Monitor:
    """Only reads the local status file and kernel resource counters."""
    def __init__(self, data: Path = DATA, models: Path = MODELS,
                 cgroup: Path = CGROUP, proc: Path = Path("/proc")):
        self.data = data
        self.models = models
        self.cgroup = cgroup
        self.proc = proc
        self.previous_cpu: tuple[int, float] | None = None
        self.previous_inference: tuple[int, int, float] | None = None
        self.cpu_history: deque[float | None] = deque(maxlen=36)
        self.memory_history: deque[float | None] = deque(maxlen=36)

    def collect(self, now: float | None = None, monotonic: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        status = read_snapshot(self.data / STATUS_FILE)
        hw = safe_dict(status.get("hardware")) if status else {}
        cap = hw.get("cpus") or 0
        cpus = float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0 else 0
        usage = cgroup_cpu_usage(self.cgroup)
        container_cpu = cpu_percent(self.previous_cpu, (usage, monotonic), cpus) if usage is not None else None
        self.previous_cpu = (usage, monotonic) if usage is not None else None
        process = llama_server_process(self.proc)
        inference_cpu = None
        if process is not None:
            prev = self.previous_inference
            if prev and prev[0] == process["pid"]:
                inference_cpu = cpu_percent(
                    (prev[1], prev[2]), (process["cpu_usec"], monotonic), cpus)
            self.previous_inference = (process["pid"], process["cpu_usec"], monotonic)
        else:
            self.previous_inference = None
        used_ram = cgroup_memory_used(self.cgroup)
        limit_ram = hw.get("memory_limit_bytes") if isinstance(hw.get("memory_limit_bytes"), int) else None
        memory_ratio = 100 * used_ram / limit_ram if used_ram is not None and limit_ram else None
        self.cpu_history.append(inference_cpu)
        self.memory_history.append(min(100, memory_ratio) if memory_ratio is not None else None)
        age = max(0.0, now - float(status.get("updated_at") or 0)) if status else None
        return {"status": status, "age": age, "fresh": age is not None and age <= 45,
                "cpu": inference_cpu, "container_cpu": container_cpu,
                "llama_rss": process["rss_bytes"] if process else None,
                "llama_pid": process["pid"] if process else None,
                "ram_used": used_ram, "ram_limit": limit_ram,
                "memory_pct": memory_ratio, "now": now}

    def resource_panels(self, sample: dict[str, Any], width: int) -> Any:
        status = sample["status"] or {}
        state = safe_dict(status.get("models"))
        cpu = sample["cpu"]
        used = state.get("disk_used_bytes")
        limit = state.get("disk_limit_bytes")
        rows = [
            ("Inference CPU", f"{cpu:.0f}%" if cpu is not None else ("Measuring…" if sample["llama_pid"] else "Not running"),
             progress_bar(cpu, 100), sparkline(self.cpu_history), "cyan"),
            ("Container CPU", f"{sample['container_cpu']:.0f}%" if sample["container_cpu"] is not None else "Measuring…",
             progress_bar(sample["container_cpu"], 100), "Includes TUI display activity", "yellow"),
            ("Container RAM", f"{human_bytes(sample['ram_used'])} / {human_bytes(sample['ram_limit'])}",
             progress_bar(sample["ram_used"], sample["ram_limit"]),
             sparkline(self.memory_history), "magenta"),
            ("Model storage", f"{human_bytes(used)} / {human_bytes(limit)}",
             progress_bar(used, limit), "Application quota · not filesystem hard limit", "green"),
        ]
        if width < 90:
            table = Table.grid(padding=(0, 2))
            table.add_column(style="bold", no_wrap=True)
            table.add_column()
            for label, values, bar, note, color in rows:
                table.add_row(label, Text(f"{values}  {bar}", style=color))
            return Panel(table, title="Resources", border_style="cyan")
        cards = []
        for label, values, bar, note, color in rows:
            body = Group(Text(values, style=f"bold {color}"),
                         Text(bar, style=color),
                         Text(note, style="dim", overflow="fold"))
            cards.append(Panel(body, title=label, border_style=color, padding=(0, 1)))
        return Columns(cards, expand=True, equal=True)

    def activity_panel(self, sample: dict[str, Any]) -> Panel:
        status = sample["status"] or {}
        phase = str(status.get("phase") or "waiting")
        current = status.get("current_model")
        state = safe_dict(status.get("models"))
        loaded = state.get("active") if state.get("ready") else None
        started = status.get("job_started_at")
        stage = status.get("phase_started_at")
        if not sample["fresh"]:
            label = "Snapshot missing or stale — worker may be stopped"
            color = "yellow"
        elif phase == "generating":
            label = f"RUNNING inference on {current or '?'}"
            color = "green"
        elif phase == "idle":
            label = f"IDLE · warmed model: {loaded or 'none'}"
            color = "cyan"
        else:
            label = f"{phase.upper()} · {current or 'no model'}"
            color = "yellow"
        table = Table.grid(expand=True)
        table.add_column(ratio=3)
        table.add_column(ratio=1, justify="right")
        table.add_row(Text(label, style=f"bold {color}"),
                      Text(elapsed(started or stage, sample["now"]), style="bold"))
        table.add_row(Text(f"Task: {status.get('current_kind') or 'none'} · CPU backend: llama.cpp",
                           style="dim"),
                      Text(f"Policy rev {state.get('policy_revision', '—')}", style="dim"))
        rss = sample.get("llama_rss")
        if rss is not None:
            # RSS is a real measurement, but mmap/page cache makes it unsuitable
            # as a truthful percent-complete signal for model initialization.
            if phase == "loading into RAM":
                table.add_row(Text("Loading: model process RAM " + human_bytes(rss) +
                                   " · waiting for llama.cpp /health", style="yellow"),
                              Text("No reliable load %", style="dim"))
            else:
                table.add_row(Text("llama-server resident RAM: " + human_bytes(rss), style="dim"),
                              Text("RSS", style="dim"))
        return Panel(table, title="Current activity", border_style=color, padding=(0, 1))

    def models_panel(self, sample: dict[str, Any]) -> Panel:
        status = sample["status"] or {}
        state = safe_dict(status.get("models"))
        caps = safe_dict(state.get("capabilities"))
        installed = set(state.get("installed") if isinstance(state.get("installed"), list) else [])
        policy = set(status.get("policy") if isinstance(status.get("policy"), list) else [])
        active = state.get("active") if state.get("ready") else None
        current = status.get("current_model")
        phase = status.get("phase", "")
        table = Table(box=box.SIMPLE_HEAVY, expand=True, show_lines=False, header_style="bold cyan")
        table.add_column("Model", ratio=3)
        table.add_column("Approved", justify="center", no_wrap=True)
        table.add_column("Test", justify="center", no_wrap=True)
        table.add_column("Disk", justify="right", no_wrap=True)
        table.add_column("RAM", justify="center", no_wrap=True)
        table.add_column("Generating", justify="center", no_wrap=True)
        table.add_column("Speed", justify="right", no_wrap=True)
        for mid, model in CATALOG.items():
            outcome = safe_dict(caps.get(mid))
            test = str(outcome.get("status") or "not tested")
            benchmark = safe_dict(outcome.get("benchmark"))
            speed = benchmark.get("generation_tps")
            speed_label = f"{speed:.1f} t/s" if isinstance(speed, (float, int)) and speed > 0 else "—"
            disk_file = self.models / f"{mid}.gguf"
            partial = self.models / f"{mid}.part"
            disk_state = "—"
            if mid in installed or disk_file.is_file():
                disk_state = "Saved"
            elif partial.is_file():
                try:
                    fraction = min(100, 100 * partial.stat().st_size / int(model["size"]))
                    disk_state = f"{fraction:.0f}%"
                except (OSError, KeyError, ZeroDivisionError):
                    disk_state = "Fetching"
            ram_state = "Loaded" if active == mid else "—"
            generating = "●" if phase == "generating" and current == mid and sample["fresh"] else "—"
            table.add_row(Text(str(model["name"]), style="bold" if generating == "●" else ""),
                          Text("Yes" if mid in policy else "—", style="green" if mid in policy else "dim"),
                          Text(test, style="green" if test == "supported" else "yellow" if test == "testing" else "dim"),
                          Text(disk_state, style="green" if disk_state == "Saved" else "yellow"),
                          Text(ram_state, style="cyan" if ram_state == "Loaded" else "dim"),
                          Text(generating, style="bold green" if generating == "●" else "dim"),
                          Text(speed_label, style="bright_white"))
        return Panel(table, title="Model catalog · disk ≠ RAM · ● current inference",
                     border_style="blue", padding=(0, 1))

    def render(self, sample: dict[str, Any], width: int = 120, interval: float = 1.0) -> Group:
        status = sample["status"] or {}
        fresh = sample["fresh"]
        connected = bool(status.get("connected")) if fresh else False
        connection = ("CONNECTED" if connected else "DISCONNECTED" if fresh else "NO LIVE DATA")
        color = "green" if connected else "yellow"
        name = str(status.get("worker_name") or "Compute Worker")[:80]
        heading = Text()
        heading.append(" SimpleAI ", style="bold white on dark_blue")
        heading.append("  COMPUTE WORKER", style="bold cyan")
        heading.append(f"    {name}", style="white")
        heading.append(f"    ● {connection}", style=f"bold {color}")
        header = Panel(heading, border_style="cyan", padding=(0, 1))
        resources = self.resource_panels(sample, width)
        freshness = "waiting for worker" if sample["age"] is None else f"last snapshot {sample['age']:.0f}s ago"
        footer = Text(f"  q  quit     r  refresh now     + faster / - slower     interval {interval:g}s    ·    {freshness}", style="dim")
        return Group(header, resources, self.activity_panel(sample),
                     self.models_panel(sample), footer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only SimpleAI Worker dashboard")
    parser.add_argument("--once", action="store_true", help="Print a static snapshot without requiring a TTY")
    args = parser.parse_args(argv)
    monitor = Monitor()
    console = Console()
    if args.once:
        console.print(monitor.render(monitor.collect(), console.width))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive dashboard requires a terminal. Use --once for a snapshot.", file=sys.stderr)
        return 2

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    interval = 1.0
    try:
        tty.setcbreak(fd)
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(monitor.render(monitor.collect(), console.width, interval), refresh=True)
                readable, _, _ = select.select([sys.stdin], [], [], interval)
                if readable:
                    key = sys.stdin.read(1).lower()
                    if key in ("q", "\x03"):
                        break
                    interval = change_interval(interval, key)
                    # r: immediately refresh on the next iteration.
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
