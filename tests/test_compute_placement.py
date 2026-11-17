"""Deterministic test coverage for automatic per-node placement and model tiers."""
import pytest

# Placement baseline tests only the immutable bundled models. Dynamic models
# are tested independently and may have been added to the process-wide catalog
# by preceding admin API tests.
from compute_catalog import BUILTIN_CATALOG as CATALOG
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
    # 8 GiB disk comfortably holds all three individually RAM-fitting models.
    assert x86 == ["qwen35-4b-q4", "qwen35-2b-q4", "qwen35-0.8b-q4"]
    assert pi == ["qwen35-2b-q4", "qwen35-0.8b-q4"]
    # 9B plus 0.8B are the models fitting the 8 GiB cache on large-RAM ARM.
    assert arm == ["qwen35-9b-q4", "qwen35-0.8b-q4"]


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
    assert auto_tier({"messages": [{"role": "user", "content": "Hi"}]}) == "fast"
    assert auto_tier({"messages": [{"role": "user", "content": "a" * 10000}]}) == "quality"
    assert auto_tier({"messages": [{"role": "user", "content": "Hi"}],
                      "aiproxy_compute_tier": "fast"}) == "fast"


def test_three_approved_models_are_all_usable_on_home_worker():
    approved = ["qwen35-0.8b-q4", "qwen35-2b-q4", "qwen35-4b-q4"]
    assigned = models_for_node(approved, CATALOG, *telemetry(6, 8))
    assert set(assigned) == set(approved)
    assert assigned == ["qwen35-4b-q4", "qwen35-2b-q4", "qwen35-0.8b-q4"]
    assert sum(max(CATALOG[mid]["size"] + 16*1024**2,
                   int(CATALOG[mid]["size"]*1.15))
               for mid in assigned) <= int(8*1024**3 * .90)


def test_auto_placement_enforces_aggregate_disk_budget_and_ram_caps():
    approved = ["qwen35-0.8b-q4", "qwen35-2b-q4", "qwen35-4b-q4"]
    restricted = models_for_node(approved, CATALOG, *telemetry(8, 3))
    assert restricted == ["qwen35-2b-q4", "qwen35-0.8b-q4"]
    assert "qwen35-4b-q4" not in restricted
    assert models_for_node(approved, CATALOG, *telemetry(3, 8)) == [
        "qwen35-2b-q4", "qwen35-0.8b-q4"
    ]


def test_auto_placement_respects_real_host_disk_space():
    hw, state = telemetry(24, 64)
    hw["disk_free_bytes"] = 700 * 1024**2
    state["disk_used_bytes"] = 0
    # Plenty of configured quota is not enough if the physical disk is full.
    assert models_for_node(list(CATALOG), CATALOG, hw, state) == ["qwen35-0.8b-q4"]


def test_auto_model_selects_privacy_eligible_workers_before_quality_tier(monkeypatch):
    import compute_plane

    monkeypatch.setattr(compute_plane, "CATALOG", CATALOG)
    monkeypatch.setattr(compute_plane, "_model_policy",
                        lambda: {"revision": 2, "models": list(CATALOG)})

    def fake_endpoints(model):
        mid = model.split("/")[-1]
        # The largest model is deliberately on a friend-owned, untrusted PC.
        return [{"api_mode": "compute_worker", "node_id": mid, "model": mid,
                 "name": mid, "allow_private": mid != "qwen35-9b-q4"}]

    monkeypatch.setattr(compute_plane, "endpoints", fake_endpoints)
    payload = {"aiproxy_compute_tier": "quality",
               "messages": [{"role": "user", "content": "confidential"}]}
    private = compute_plane.automatic_endpoints(payload)
    assert [item["model"] for item in private] == ["qwen35-4b-q4"]
    public = compute_plane.automatic_endpoints({
        **payload, "aiproxy_visibility": "public"})
    assert [item["model"] for item in public] == ["qwen35-9b-q4"]
    with pytest.raises(ValueError, match="aiproxy_visibility"):
        compute_plane.automatic_endpoints({**payload, "aiproxy_visibility": "maybe"})


def test_auto_model_rejects_private_tier_with_only_untrusted_matching_node(monkeypatch):
    import compute_plane

    monkeypatch.setattr(compute_plane, "CATALOG", CATALOG)
    monkeypatch.setattr(compute_plane, "_model_policy",
                        lambda: {"revision": 2, "models": list(CATALOG)})
    monkeypatch.setattr(compute_plane, "endpoints", lambda model: [{
        "api_mode": "compute_worker", "node_id": model, "model": model.split("/")[-1],
        "name": model, "allow_private": model.endswith("0.8b-q4")
    }])
    assert compute_plane.automatic_endpoints({"aiproxy_compute_tier": "quality"}) == []
