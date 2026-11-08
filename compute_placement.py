"""Conservative per-node model placement, limited by RAM and aggregate GGUF disk.

Global approval means a model *may* be used. Automatic mode assigns as many
approved models as fit a given worker's individual RAM limits and cumulative
cache budget; only one is loaded in llama.cpp RAM at a time.
"""
from __future__ import annotations

from typing import Any

GIB = 1024 ** 3
MIB = 1024 ** 2


def models_for_node(selected: list[str], catalog: dict[str, dict],
                    hardware: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Return all fitting models (largest first) without exceeding cache budget.

    Individual RAM fit is checked for each model, not the sum: CPU worker
    loads exactly one model at a time. Disk fit is cumulative and reserves
    headroom for downloads. Unknown telemetry preserves manual approval until
    the first real heartbeat; unsupported models are still filtered by the
    worker's actual inference self-test.
    """
    limit = hardware.get("memory_limit_bytes", 0)
    disk_limit = state.get("disk_limit_bytes", 0)
    if (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
            or not isinstance(disk_limit, int) or isinstance(disk_limit, bool) or disk_limit <= 0):
        return list(selected)
    reserve = min(2 * GIB, max(512 * MIB, limit // 8))
    headroom = max(128 * MIB, min(256 * MIB, limit // 8))
    permitted = []
    for mid in selected:
        item = catalog.get(mid)
        if not isinstance(item, dict):
            continue
        size = item.get("size")
        if type(size) is not int or size <= 0:
            continue
        required_ram = int(size * 1.30) + reserve + headroom
        required_disk = max(size + 16 * MIB, int(size * 1.15))
        if required_ram <= limit and required_disk <= disk_limit:
            permitted.append((mid, size, required_disk))
    # Larger models retain priority when capacity is scarce. Smaller fitting
    # models are also included, not silently limited to a fixed count of 2.
    permitted.sort(key=lambda entry: entry[1], reverse=True)
    budget = int(disk_limit * 0.90)
    chosen: list[str] = []
    for mid, _, required_disk in permitted:
        if required_disk <= budget:
            chosen.append(mid)
            budget -= required_disk
    return chosen
