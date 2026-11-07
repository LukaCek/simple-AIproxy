"""Conservative per-node CPU model placement. Runs only on administrator-approved catalog entries."""
from __future__ import annotations

from typing import Any

GIB = 1024 ** 3
MIB = 1024 ** 2


def models_for_node(selected: list[str], catalog: dict[str, dict],
                    hardware: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Pick a large capable model and a smaller low-latency fallback per node.

    Unknown telemetry keeps the approved list until first heartbeat. This
    function never approves a model not present in the global policy.
    """
    limit = hardware.get("memory_limit_bytes", 0)
    disk_limit = state.get("disk_limit_bytes", 0)
    if (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
            or not isinstance(disk_limit, int) or isinstance(disk_limit, bool) or disk_limit <= 0):
        return list(selected)
    reserve = min(2 * GIB, max(512 * MIB, limit // 8))
    headroom = max(128 * MIB, min(256 * MIB, limit // 8))
    allowed = []
    for mid in selected:
        item = catalog.get(mid)
        if not item:
            continue
        size = item["size"]
        if int(size * 1.30) + reserve + headroom <= limit and int(size * 1.15) <= disk_limit:
            allowed.append(mid)
    if not allowed:
        return []
    allowed.sort(key=lambda mid: catalog[mid]["size"], reverse=True)
    primary = allowed[0]
    chosen = [primary]
    # Keep a fast model on each node where it fits; 2B is preferred over 0.8B.
    # The total on-disk budget must be sufficient for both, with headroom.
    remaining = int(disk_limit * 0.90) - int(catalog[primary]["size"])
    alternatives = [mid for mid in allowed[1:] if int(catalog[mid]["size"] * 1.05) <= remaining]
    if alternatives:
        target = min(2 * GIB, catalog[primary]["size"] * .45)
        secondary = min(alternatives, key=lambda mid: abs(catalog[mid]["size"] - target))
        chosen.append(secondary)
    return chosen
