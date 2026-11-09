"""Bounded server-side diagnostics for MCP (no Docker socket, no secrets).

Collect only fixed log levels, logger namespaces and error classes/codes.
Raw log record messages and tracebacks may contain API keys and user prompts,
so never retain or export them. Request and provider attempts are read from
their existing SQLite tables by the MCP tools, with sensitive columns excluded.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
from threading import Lock
from typing import Any

_events: deque[dict[str, Any]] = deque(maxlen=400)
_mutex = Lock()
_started = datetime.now(timezone.utc).isoformat()
_attached = False


def record(component: str, level: str, code: str, *, exception_type: str = "") -> None:
    # Strict normalized fields only: never any arbitrary exception.message,
    # request body, URL, token, traceback or stdout from the backend.
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    def safe(s: str) -> str:
        return "".join(c if c in allowed else "_" for c in str(s))[:64]
    entry = {"at": datetime.now(timezone.utc).isoformat(),
             "component": safe(component), "level": safe(level),
             "code": safe(code), "exception_type": safe(exception_type)}
    with _mutex:
        _events.append(entry)


class _SafeHandler(logging.Handler):
    def emit(self, record_: logging.LogRecord) -> None:
        try:
            if record_.levelno < logging.WARNING:
                return
            exc_type = record_.exc_info[0].__name__ if record_.exc_info else ""
            record(record_.name, record_.levelname.lower(),
                   "python_exception" if record_.exc_info else "log_warning",
                   exception_type=exc_type)
        except Exception:
            pass


def install() -> None:
    global _attached
    if _attached:
        return
    _attached = True
    handler = _SafeHandler()
    handler.setLevel(logging.WARNING)
    for name in ("uvicorn.error", "main_impl", "compute_plane", "httpx",
                 "responses_streaming_entrypoint", "codex_registry"):
        logging.getLogger(name).addHandler(handler)
    record("simpleai_mcp", "info", "diagnostic_collector_started")


def events(*, level: str = "", limit: int = 100) -> dict[str, Any]:
    if level not in ("", "warning", "error", "critical"):
        raise ValueError("Invalid severity")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Limit must be between 1 and 200")
    with _mutex:
        entries = [e for e in reversed(_events)
                   if not level or e["level"] == level][:limit]
    return {"started_at": _started, "retention": "in-memory, 400 events",
            "source": "safe structured Python logging categories; not raw Docker logs",
            "events": entries}
