"""Published, source-attributed Codex model benchmark snapshots.

Scores are not a universal intelligence scale. A comparison is valid ONLY
within the same named metric and version. Unpublished/new models have no
benchmarks until a verified data point is added. Never guess scores.
"""
from __future__ import annotations

from typing import Any

METRICS = {
    "automation": {
        "label": "AutomationBench",
        "edition": "AutomationBench (published results, 2026)",
        "unit": "%",
        "purpose": "Agent workflows and automation (Hermes)",
        "description": "Multi-step tool workflows. A proxy for Hermes-style agents, not a direct Hermes benchmark.",
    },
    "coding": {
        "label": "DeepSWE v1.1",
        "edition": "DeepSWE v1.1",
        "unit": "%",
        "purpose": "Coding and repository changes",
        "description": "Real-repository software tasks. Harness and reasoning-effort differences can affect results.",
    },
    "general": {
        "label": "Artificial Analysis Intelligence Index",
        "edition": "Intelligence Index v4.1",
        "unit": "/100",
        "purpose": "General intelligence",
        "description": "The same v4.1 edition, not comparable to later index revisions.",
    },
    "tools": {
        "label": "Toolathlon",
        "edition": "Toolathlon, GPT-5.6 launch results",
        "unit": "%",
        "purpose": "Tool use",
        "description": "Tool execution benchmark, reported for the GPT-5.6 family.",
    },
}

SRC_56 = "https://openai.com/index/gpt-5-6/"
SRC_ASTRA = "https://openai.com/index/gpt-6-astra/"
SRC_6 = "https://openai.com/index/introducing-gpt-6-sol-and-luna/"
SRC_61 = "https://openai.com/index/introducing-gpt-6-1-sol/"

# One row is one source-supported observation; do not mix v4.1 and v4.1.1
# intelligence indices, or different coding-agent-index revisions.
BENCHMARKS: dict[str, dict[str, tuple[float, str]]] = {
    "gpt-5.6-sol": {
        "automation": (18.1, SRC_56),
        "coding": (72.7, SRC_56),
        "general": (58.9, SRC_56),
        "tools": (58.0, SRC_56),
    },
    "gpt-5.6-terra": {
        "automation": (15.2, SRC_56),
        "coding": (69.6, SRC_56),
        "general": (55.0, SRC_56),
        "tools": (53.1, SRC_56),
    },
    "gpt-5.6-luna": {
        "automation": (14.9, SRC_56),
        "coding": (67.2, SRC_56),
        "general": (51.2, SRC_56),
        "tools": (53.4, SRC_56),
    },
    "gpt-6-astra": {
        "automation": (41.4, SRC_ASTRA),
        "coding": (74.1, SRC_ASTRA),
    },
    "gpt-6-sol": {
        "automation": (33.2, SRC_6),
        "coding": (68.8, SRC_6),
    },
    "gpt-6-luna": {
        "coding": (66.6, SRC_6),
    },
}


def model_benchmarks(slug: str) -> dict[str, dict[str, Any]]:
    return {
        metric: {
            "score": score,
            "source": source,
            "edition": METRICS[metric]["edition"],
        }
        for metric, (score, source) in BENCHMARKS.get(slug, {}).items()
    }


def benchmark_metadata() -> dict[str, Any]:
    return {
        "metrics": METRICS,
        "note": (
            "Scores are published benchmark snapshots, not live Hermes results "
            "or guaranteed production performance. Prices estimate the same "
            "token scenario, not equal task costs. Unpublished models have no score."
        ),
        "updated_at": "2026-10-25",
    }
