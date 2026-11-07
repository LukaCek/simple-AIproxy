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
