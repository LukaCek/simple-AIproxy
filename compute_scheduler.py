"""Deterministic, conservative routing for CPU compute endpoints.

No model substitution takes place here. Non-compute providers retain their
relative positions; compute candidates are scored only within their own slots.
Worker telemetry is untrusted and missing measurements are not fabricated.
"""
from __future__ import annotations

import json
import math
from typing import Any


def _safe(value: Any, default: float, lower: float = 0.0, upper: float = 1_000_000.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value) if lower <= value <= upper else default


def estimate_tokens(payload: dict[str, Any]) -> tuple[int, int]:
    """Approximate workload from request size; never pretend this is a tokenizer."""
    messages = payload.get("messages") or []
    try:
        serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = ""
    # A conservative sizing hint, *not* an exact context-window check.
    input_tokens = min(1_000_000, max(1, math.ceil(len(serialized.encode("utf-8")) / 3)))
    raw_output = payload.get("max_completion_tokens", payload.get("max_tokens", 256))
    output_tokens = int(_safe(raw_output, 256, 1, 16384))
    return input_tokens, output_tokens


def predict_latency(endpoint: dict[str, Any], payload: dict[str, Any]) -> float:
    """Lower is preferable. Unknown speed gets a neutral conservative prior."""
    meta = endpoint.get("compute_metrics", {})
    if not isinstance(meta, dict):
        meta = {}
    bench = meta.get("benchmark", {})
    if not isinstance(bench, dict):
        bench = {}
    inp, out = estimate_tokens(payload)
    prompt_tps = _safe(bench.get("prompt_tps"), 15.0, 0.01)
    gen_tps = _safe(bench.get("generation_tps"), 5.0, 0.01)
    latency = inp / prompt_tps + out / gen_tps
    if not meta.get("warm"):
        latency += _safe(bench.get("load_seconds"), 12.0, 0.0)
    if meta.get("busy"):
        latency += 10_000.0
    # Existing provider routing may rotate equally fast nodes.
    return latency


def order_candidates(endpoints: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve configured cloud-provider positions and rank compute slots only."""
    slots = [i for i, endpoint in enumerate(endpoints) if endpoint.get("api_mode") == "compute_worker"]
    if len(slots) < 2:
        return list(endpoints)
    result = list(endpoints)
    ranked = sorted((result[i] for i in slots), key=lambda e: predict_latency(e, payload))
    for i, endpoint in zip(slots, ranked):
        result[i] = endpoint
    return result


def auto_tier(payload: dict[str, Any]) -> str:
    """Explicit quality requests win over conservative length-based defaults."""
    tier = payload.get("aiproxy_compute_tier", "auto")
    if tier not in ("auto", "fast", "balanced", "quality"):
        raise ValueError("aiproxy_compute_tier must be auto, fast, balanced or quality")
    if tier != "auto":
        return tier
    inp, out = estimate_tokens(payload)
    if inp < 512 and out <= 512:
        return "fast"
    if inp < 1600 and out <= 768:
        return "balanced"
    return "quality"


def auto_candidates(endpoints: list[dict[str, Any]], payload: dict[str, Any],
                    catalog: dict[str, dict]) -> list[dict[str, Any]]:
    """Select one catalog model, then schedule only equivalent replicas.

    Explicit 'quality' never degrades to a tiny model. Automatic selection is
    heuristic, not a reliable measure of semantic difficulty.
    """
    tier = auto_tier(payload)
    size_by_id = {mid: v.get("size", 0) for mid, v in catalog.items()}
    available = {e["model"] for e in endpoints if e.get("model") in size_by_id}
    if not available:
        return []
    gib = 1024 ** 3
    eligible = [mid for mid in available if (
        (tier == "fast" and size_by_id[mid] <= 1.5 * gib) or
        (tier == "balanced" and 1.0 * gib <= size_by_id[mid] <= 3.5 * gib) or
        (tier == "quality" and size_by_id[mid] >= 2.5 * gib)
    )]
    if not eligible:
        if payload.get("aiproxy_compute_tier", "auto") != "auto":
            return []
        # Automatic mode can use a weaker available model; explicit tiers cannot.
        eligible = list(available)
    chosen = max(eligible, key=lambda mid: size_by_id[mid])
    return order_candidates([e for e in endpoints if e["model"] == chosen], payload)
