"""Compute scheduler regressions: selection never changes the requested model."""
import asyncio

import compute_scheduler as scheduler
import compute_plane


def candidate(node, model="qwen35-2b-q4", prompt=20, gen=5, warm=True, busy=False):
    return {"api_mode": "compute_worker", "name": node, "node_id": node,
            "model": model, "compute_metrics": {"warm": warm, "busy": busy,
                                                "benchmark": {"prompt_tps": prompt,
                                                              "generation_tps": gen,
                                                              "load_seconds": 8}}}


def test_measured_fast_worker_wins_for_same_model():
    slow = candidate("arm", gen=2)
    fast = candidate("x86", gen=16)
    chosen = scheduler.order_candidates([slow, fast],
                 {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 128})
    assert [c["name"] for c in chosen] == ["x86", "arm"]
    assert {c["model"] for c in chosen} == {"qwen35-2b-q4"}


def test_warm_and_idle_preferred_and_cloud_positions_unchanged():
    cold = candidate("cold", gen=10, warm=False)
    warm = candidate("warm", gen=10)
    cloud = {"api_mode": "openai_chat_completions", "name": "cloud"}
    out = scheduler.order_candidates([cold, cloud, warm], {"messages": [], "max_tokens": 1})
    assert [c["name"] for c in out] == ["warm", "cloud", "cold"]
    busy = candidate("busy", gen=100, busy=True)
    assert scheduler.order_candidates([busy, warm], {"messages": []})[0]["name"] == "warm"


def test_untrusted_or_missing_telemetry_is_not_treated_as_fast():
    unknown = candidate("unknown")
    unknown["compute_metrics"]["benchmark"] = {"generation_tps": float("nan"), "prompt_tps": -100}
    fast = candidate("fast", gen=100, prompt=100)
    assert scheduler.order_candidates([unknown, fast], {"messages": []})[0]["name"] == "fast"
    assert scheduler.estimate_tokens({"messages": [{"role": "user", "content": "Hello"}]})[0] > 0


def test_wait_limit_is_bounded(monkeypatch):
    monkeypatch.setenv("AIPROXY_COMPUTE_QUEUE_WAIT_SECONDS", "50")
    assert compute_plane._wait_timeout() == 50.0
    for invalid in ("nan", "inf", "9000", "-1", "oops"):
        monkeypatch.setenv("AIPROXY_COMPUTE_QUEUE_WAIT_SECONDS", invalid)
        assert compute_plane._wait_timeout() == 10.0


def test_atomic_single_slot_reservation_and_release():
    async def run():
        node = "compute-test-single-slot"
        compute_plane._connections[node] = object()
        try:
            a, b = await asyncio.gather(
                compute_plane._reserve(node, timeout=0),
                compute_plane._reserve(node, timeout=0))
            assert (a is None) != (b is None)
            claimed = a or b
            assert node in compute_plane._reservations
            compute_plane._release(node, "wrong-owner")
            assert node in compute_plane._reservations
            compute_plane._release(node, claimed)
            assert node not in compute_plane._reservations
            again = await compute_plane._reserve(node, timeout=0)
            assert again
            compute_plane._release(node, again)
        finally:
            compute_plane._connections.pop(node, None)
            compute_plane._reservations.pop(node, None)
    asyncio.run(run())
