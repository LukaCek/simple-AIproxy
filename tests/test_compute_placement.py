"""Deterministic test coverage for automatic per-node placement and model tiers."""
import pytest

from compute_catalog import CATALOG
from compute_placement import models_for_node
from compute_scheduler import auto_candidates, auto_tier


def telemetry(ram_gib, disk_gib=8):
    return ({"memory_limit_bytes": ram_gib * 1024**3},
            {"disk_limit_bytes": disk_gib * 1024**3})


def test_automatic_placement_matches_ram_and_disk_constraints():
    selected = list(CATALOG)
    arm = models_for_node(selected, CATALOG, *telemetry(24))
    x86 = models_for_node(selected, CATALOG, *telemetry(8))
    pi = models_for_node(selected, CATALOG, *telemetry(4))
    assert arm[0] == "qwen35-9b-q4"
    assert "qwen35-9b-q4" not in x86
    assert x86[0] == "qwen35-4b-q4"
    assert "qwen35-4b-q4" not in pi
    assert pi[0] in ("qwen35-2b-q4", "qwen35-0.8b-q4")
    assert len(arm) <= 2 and len(x86) <= 2 and len(pi) <= 2


def test_placement_respects_approved_list_and_unknown_telemetry():
    selected = ["qwen35-0.8b-q4"]
    assert models_for_node(selected, CATALOG, *telemetry(24)) == selected
    assert models_for_node(selected, CATALOG, {}, {}) == selected
    assert models_for_node(selected, CATALOG, *telemetry(1)) == []


def test_auto_alias_uses_one_model_and_honors_explicit_tier():
    pool = [{"api_mode": "compute_worker", "model": mid,
             "node_id": "n-" + mid, "name": mid} for mid in CATALOG]
    for tier, expected in (
        ("fast", "qwen35-2b-q4"),
        ("balanced", "qwen35-4b-q4"),
        ("quality", "qwen35-9b-q4"),
    ):
        result = auto_candidates(pool, {"aiproxy_compute_tier": tier,
                                         "messages": [{"role": "user", "content": "Hi"}]}, CATALOG)
        assert len(result) == 1 and result[0]["model"] == expected
    assert auto_candidates(pool[:2], {"aiproxy_compute_tier": "quality"}, CATALOG) == []
    with pytest.raises(ValueError):
        auto_tier({"aiproxy_compute_tier": "invented"})


def test_auto_model_input_is_heuristic_not_forced_on_explicit_model():
    assert auto_tier({"messages": [{"role": "user", "content": "a" * 10000}]}) == "quality"
    assert auto_tier({"messages": [{"role": "user", "content": "Hi"}],
                      "aiproxy_compute_tier": "fast"}) == "fast"
