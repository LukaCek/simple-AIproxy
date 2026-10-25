"""Source/edition safety and client chart regressions for Codex benchmarks."""
import re
import shutil
import subprocess
from pathlib import Path

import codex_benchmarks as bench


def test_published_gpt56_results_and_sources():
    models = [
        ("gpt-5.6-sol", 58.9, 72.7, 18.1, 58.0),
        ("gpt-5.6-terra", 55.0, 69.6, 15.2, 53.1),
        ("gpt-5.6-luna", 51.2, 67.2, 14.9, 53.4),
    ]
    for name, intelligence, coding, agents, tools in models:
        data = bench.model_benchmarks(name)
        assert data["general"]["score"] == intelligence
        assert data["coding"]["score"] == coding
        assert data["automation"]["score"] == agents
        assert data["tools"]["score"] == tools
        assert all(item["source"].startswith("https://openai.com/") for item in data.values())
        assert all(item["edition"] for item in data.values())


def test_new_models_are_unscored_until_evidence_exists():
    assert bench.model_benchmarks("gpt-7") == {}
    # No numeric inference from prose-only release claims.
    assert bench.model_benchmarks("gpt-6.1-sol") == {}


def test_different_index_editions_are_not_falsely_compared():
    assert "general" not in bench.model_benchmarks("gpt-6-astra")
    assert bench.model_benchmarks("gpt-6-astra")["coding"]["score"] == 74.1
    assert bench.model_benchmarks("gpt-6-sol")["automation"]["score"] == 33.2


def test_metric_labels_and_methods_explain_limits():
    meta = bench.benchmark_metadata()
    assert set(meta["metrics"]) == {"automation", "coding", "general", "tools"}
    assert "Hermes" in meta["metrics"]["automation"]["purpose"]
    assert "not live Hermes" in meta["note"]


def test_admin_ui_renders_graphs_sources_and_accessible_table():
    f = Path(__file__).resolve().parents[1] / "templates" / "codex_usage.html"
    text = f.read_text()
    assert "benchmark-scatter" in text
    assert "benchmark-value" in text
    assert "benchmark-table" in text
    assert "benchmark-metric" in text
    assert "benchmark-available" in text
    assert "Available on my accounts" in text
    assert "published benchmark" in text.lower()
    assert "renderComparison()" in text
    match = re.search(r"<script>(.*?)</script>", text, flags=re.S)
    assert match
    if shutil.which("node"):
        parsed = subprocess.run(["node", "--check"], input=match.group(1),
                                capture_output=True, text=True, check=False)
        assert parsed.returncode == 0, parsed.stderr
