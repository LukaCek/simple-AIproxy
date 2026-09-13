#!/usr/bin/env python3
"""Fetch live Simple AIproxy Codex limits for Omarchy's Agents panel."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "omarchy" / "agents" / "simple-aiproxy.json"
DEFAULT_OUTPUT_PATH = Path.home() / ".local" / "state" / "omarchy" / "agents" / "usage" / "aiproxy.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing configuration: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read configuration: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("Simple AIproxy configuration must be a JSON object")
    return data


def _dotenv_value(path: Path, variable_name: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Could not read API key environment file: {exc}") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or name.strip() != variable_name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return ""


def resolve_api_key(config: dict[str, Any]) -> str:
    api_key = str(os.getenv("AIPROXY_API_KEY") or config.get("apiKey") or "").strip()
    if api_key:
        return api_key
    env_file = str(config.get("apiKeyEnvFile") or "").strip()
    if not env_file:
        return ""
    variable_name = str(config.get("apiKeyEnvName") or "AI_API_KEY").strip()
    return _dotenv_value(Path(env_file).expanduser(), variable_name).strip()


def fetch_usage(config: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    base_url = str(config.get("baseUrl") or "").strip().rstrip("/")
    api_key = resolve_api_key(config)
    if not base_url:
        raise RuntimeError("Simple AIproxy baseUrl is not configured")
    if not api_key:
        raise RuntimeError("Simple AIproxy apiKey is not configured")

    request = urllib.request.Request(
        f"{base_url}/v1/codex/usage",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "omarchy-agent-usage-aiproxy/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("Simple AIproxy API key was rejected") from exc
        raise RuntimeError(f"Simple AIproxy usage request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Simple AIproxy usage request failed: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise TypeError("Simple AIproxy returned an invalid usage response")
    return payload


def _account_label(item: dict[str, Any], labels: dict[str, Any]) -> str:
    provider = str(item.get("provider") or "Codex").strip()
    configured = str(labels.get(provider) or "").strip()
    return configured or provider


def _window_title(account: str, window: dict[str, Any], prefix: str = "") -> str:
    kind = str(window.get("kind") or "").lower()
    label = str(window.get("label") or "Limit").strip()
    if kind == "primary":
        window_name = "Session"
    elif kind == "secondary":
        window_name = "Weekly" if "week" in label.lower() else label
    else:
        window_name = label
    parts = [account]
    if prefix:
        parts.append(prefix)
    parts.append(window_name)
    return " · ".join(parts)


def _append_window(
    limits: list[dict[str, Any]],
    account: str,
    window: Any,
    prefix: str = "",
) -> None:
    if not isinstance(window, dict) or window.get("used_percent") is None:
        return
    try:
        percent = max(0.0, min(1.0, float(window["used_percent"]) / 100.0))
    except (TypeError, ValueError):
        return
    limits.append(
        {
            "title": _window_title(account, window, prefix),
            "label": str(window.get("label") or "Limit"),
            "percent": percent,
            "resetsAt": str(window.get("reset_iso") or ""),
        }
    )


def build_record(payload: dict[str, Any], labels: dict[str, Any] | None = None) -> dict[str, Any]:
    labels = labels or {}
    limits: list[dict[str, Any]] = []
    errors: list[str] = []
    plans: set[str] = set()
    subscriptions = 0

    for raw_item in payload.get("data", []):
        if not isinstance(raw_item, dict):
            continue
        subscriptions += 1
        account = _account_label(raw_item, labels)
        if raw_item.get("ok") is not True:
            error = raw_item.get("error") if isinstance(raw_item.get("error"), dict) else {}
            errors.append(str(error.get("message") or f"{account} usage is unavailable"))
            continue

        usage = raw_item.get("usage") if isinstance(raw_item.get("usage"), dict) else {}
        plan = str(usage.get("plan_type") or "").strip()
        if plan and plan != "unknown":
            plans.add(plan)
        for window in usage.get("windows", []):
            _append_window(limits, account, window)
        for additional in usage.get("additional_rate_limits", []):
            if not isinstance(additional, dict):
                continue
            name = str(additional.get("name") or "Additional limit").strip()
            for window in additional.get("windows", []):
                _append_window(limits, account, window, name)

    status = ""
    help_text = ""
    if errors:
        status = f"{len(errors)} of {subscriptions} subscriptions unavailable"
        help_text = "; ".join(errors)
    elif subscriptions and not limits:
        status = "Codex limits unavailable"
        help_text = "OpenAI returned no rate-limit windows"

    # A negative sentinel keeps an all-error record discoverable without drawing
    # a fake usage meter in Omarchy's panel.
    visible_limits = limits or [{"label": "Unavailable", "percent": -1, "resetsAt": ""}]
    tier = f"{subscriptions} Codex subscription{'s' if subscriptions != 1 else ''}"
    if len(plans) == 1:
        tier += f" · {next(iter(plans))}"

    return {
        "schemaVersion": 1,
        "id": "aiproxy",
        "name": "Simple AIproxy",
        "updatedAt": str(payload.get("fetched_at") or utc_now()),
        "ready": bool(limits),
        "scope": "account",
        "hasLocalStats": False,
        "hasPromptStats": False,
        "todayPrompts": 0,
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [],
        "totalPrompts": 0,
        "totalSessions": 0,
        "activeDays": 0,
        "activeDates": [],
        "modelUsage": {},
        "limits": visible_limits,
        "tierLabel": tier,
        "usageStatusText": status,
        "authHelpText": help_text,
    }


def error_record(message: str) -> dict[str, Any]:
    record = build_record({"fetched_at": utc_now(), "data": []})
    record["tierLabel"] = "Codex subscriptions"
    record["usageStatusText"] = "Simple AIproxy unavailable"
    record["authHelpText"] = message
    return record


def write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        temporary.chmod(0o644)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        payload = fetch_usage(config)
        labels = config.get("labels") if isinstance(config.get("labels"), dict) else {}
        record = build_record(payload, labels)
        exit_code = 0
    except (RuntimeError, TypeError) as exc:
        record = error_record(str(exc))
        exit_code = 1

    write_record(args.output, record)
    if args.stdout:
        json.dump(record, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
