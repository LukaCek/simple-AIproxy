"""Bounded server-side diagnostics for MCP (no Docker socket, no secrets).

Collect only fixed log levels, logger namespaces and error classes/codes.
Raw log record messages and tracebacks may contain API keys and user prompts,
so never retain or export them. Request and provider attempts are read from
their existing SQLite tables by the MCP tools, with sensitive columns excluded.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any

_events: deque[dict[str, Any]] = deque(maxlen=400)
_mutex = Lock()
_started = datetime.now(timezone.utc).isoformat()
_attached = False
_LOG_FILE = Path(os.getenv("SIMPLEAI_MCP_RUNTIME_LOG_PATH", str(Path(__file__).resolve().parent / "cache" / "mcp-runtime-events.jsonl")))
_MAX_LOG_BYTES = 512 * 1024


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
        # Persist sanitized structure in the existing cache volume across
        # proxy restarts. Never write raw log messages or exception values.
        try:
            _LOG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_LOG_BYTES:
                rotated = _LOG_FILE.with_suffix(".1.jsonl")
                _LOG_FILE.replace(rotated)
            fd = os.open(_LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            # Telemetry must never break a real proxy request.
            pass


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
        # Include events from previous process starts, if the cache volume
        # survives an image deployment. Only accept our compact fixed schema.
        entries = []
        for path in (_LOG_FILE.with_suffix(".1.jsonl"), _LOG_FILE):
            try:
                if not path.is_file() or path.stat().st_size > _MAX_LOG_BYTES * 2:
                    continue
                for line in path.read_text(encoding="utf-8").splitlines()[-800:]:
                    record_ = json.loads(line)
                    if (isinstance(record_, dict) and
                        set(record_) == {"at","component","level","code","exception_type"} and
                        all(isinstance(v,str) and len(v)<=64 for k,v in record_.items()
                            if k!="at")):
                        entries.append(record_)
            except (OSError, UnicodeError, ValueError):
                pass
        if not entries:
            entries = list(_events)
        entries = [e for e in reversed(entries)
                   if not level or e["level"] == level][:limit]
    return {"started_at": _started, "retention": "rotating sanitized cache logs, 512 KiB + previous segment",
            "source": "safe structured Python logging categories; not raw Docker logs",
            "events": entries}
