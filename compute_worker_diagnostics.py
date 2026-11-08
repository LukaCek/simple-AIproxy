"""Safe, structured Compute Worker diagnostics shared by proxy and worker.

Only allowlisted event and reason codes cross the WSS boundary. Never forward
raw Docker/llama.cpp logs, prompts, responses, URLs, exception text or tokens:
all of those may contain private user information. Workers retain unacked
sanitized events for reconnect replay.
"""
from __future__ import annotations

import re
import time

KINDS = frozenset(("lifecycle", "network", "policy", "model", "model_test",
                   "operation", "inference"))
LEVELS = frozenset(("info", "warning", "error"))
CODES = frozenset((
    "starting", "connecting", "connected", "disconnected", "idle",
    "policy_received", "invalid_policy", "download_start", "load_start",
    "unload_start", "benchmark_start", "test_start", "test_supported",
    "test_unsupported", "test_failed", "inference_start", "inference_end",
    "inference_failed", "operation_start", "operation_succeeded",
    "operation_failed", "ram_limit", "ram_available", "disk_limit",
    "disk_full", "download_forbidden", "download_not_found",
    "download_failed", "checksum_mismatch", "llama_crashed",
    "llama_not_ready", "llama_http_error", "timeout", "transport_error",
    "worker_busy", "unknown_error", "backend_unsupported", "backend_load_failed",
    "backend_out_of_memory", "backend_chat_template_error",
))
METRICS = frozenset(("ram_limit_mib", "ram_available_mib", "disk_limit_mib",
                     "disk_used_mib", "model_size_mib"))
_MODEL_ID = re.compile(r"[a-z][a-z0-9._-]{1,63}\Z")


def error_code(reason: object) -> str:
    """Map *untrusted* exception text to a fixed safe code, not a substring."""
    s = str(reason).lower()[:4096]
    if "sha256" in s or "checksum" in s or "hash mismatch" in s:
        return "checksum_mismatch"
    if "insufficient" in s and ("memory" in s or "ram" in s):
        return "ram_available"
    if "memory" in s or "ram limit" in s or "cgroup" in s:
        return "ram_limit"
    if "no space" in s or "disk full" in s or "errno 28" in s:
        return "disk_full"
    if "disk limit" in s or "disk quota" in s or "storage quota" in s:
        return "disk_limit"
    if "403" in s or "forbidden" in s:
        return "download_forbidden"
    if "404" in s or "not found" in s:
        return "download_not_found"
    if "llama-server crashed" in s or "process exited" in s:
        return "llama_crashed"
    if "llama-server did not become ready" in s:
        return "llama_not_ready"
    if "llama.cpp http" in s:
        return "llama_http_error"
    if "timed out" in s or "timeout" in s:
        return "timeout"
    if "download" in s or "huggingface" in s or "httpx" in s:
        return "download_failed"
    if "connection" in s or "websocket" in s or "tls" in s or "dns" in s:
        return "transport_error"
    if "busy" in s:
        return "worker_busy"
    return "unknown_error"


def backend_error_code(line: bytes) -> str | None:
    """Extract known llama.cpp failure classes without ever exposing stderr."""
    # Backend output can contain chat prompts or generated tokens. Do not
    # persist, transmit or display any raw line, even after redaction.
    text = line[:2048].decode("utf-8", "replace").lower()
    if "out of memory" in text or "bad_alloc" in text or "failed to allocate" in text:
        return "backend_out_of_memory"
    if ("unknown model architecture" in text or "unsupported model" in text
            or "unknown tensor" in text or "unsupported architecture" in text
            or "not supported by" in text):
        return "backend_unsupported"
    if ("chat template" in text and ("error" in text or "failed" in text)
            or "jinja" in text and "error" in text):
        return "backend_chat_template_error"
    if "failed to load model" in text or "error loading model" in text:
        return "backend_load_failed"
    return None


def validate(entry: dict, *, now: int | None = None) -> dict:
    """Strictly validate worker-origin fields before disk/DB persistence."""
    if not isinstance(entry, dict):
        raise ValueError("Not a diagnostic record")
    seq = entry.get("seq")
    kind = entry.get("kind")
    code = entry.get("code")
    level = entry.get("level")
    model = entry.get("model", "")
    occurred_at = entry.get("occurred_at")
    if type(seq) is not int or not 1 <= seq < 2**53:
        raise ValueError("Bad diagnostic sequence")
    if kind not in KINDS or code not in CODES or level not in LEVELS:
        raise ValueError("Unknown diagnostic kind, code or severity")
    if not isinstance(model, str) or (model and not _MODEL_ID.fullmatch(model)):
        raise ValueError("Invalid diagnostic model ID")
    if type(occurred_at) is not int:
        raise ValueError("Invalid diagnostic timestamp")
    current = int(time.time()) if now is None else now
    if not current - 7 * 86400 <= occurred_at <= current + 600:
        raise ValueError("Diagnostic outside replay window")
    raw_metrics = entry.get("metrics", {})
    if (not isinstance(raw_metrics, dict) or len(raw_metrics) > 5
            or any(k not in METRICS or type(v) is not int or
                   not 0 <= v <= 2_000_000 for k, v in raw_metrics.items())):
        raise ValueError("Unexpected diagnostic numeric metrics")
    # Discard all unknown keys: a malicious node cannot inject free text.
    return {
        "seq": seq, "kind": kind, "code": code, "level": level,
        "model": model, "occurred_at": occurred_at,
        "metrics": dict(raw_metrics),
    }


def describe(entry: dict) -> str:
    """Only proxy-generated labels, numbers and known codes reach the UI."""
    fields = [entry["kind"] + "/" + entry["code"]]
    metrics = entry["metrics"]
    aliases = {"ram_limit_mib": "ram_cap", "ram_available_mib": "ram_free",
               "disk_limit_mib": "disk_cap", "disk_used_mib": "disk_used",
               "model_size_mib": "weights"}
    if metrics:
        fields.extend(aliases[key] + "=" + str(metrics[key]) + "MiB"
                      for key in sorted(metrics))
    return " ".join(fields)
