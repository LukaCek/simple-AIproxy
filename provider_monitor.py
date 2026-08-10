import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx


@dataclass
class _ProviderState:
    connected: Optional[bool] = None
    consecutive_failures: int = 0
    disconnect_notified: bool = False


class ProviderDisconnectMonitor:
    """Track provider health transitions and emit disconnect/recovery events."""

    def __init__(self, failure_threshold: int = 2, notify_recovery: bool = True) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.notify_recovery = bool(notify_recovery)
        self._states: Dict[str, _ProviderState] = {}

    def record(self, provider: str, success: bool, error: str = "") -> List[Dict[str, str]]:
        state = self._states.setdefault(provider, _ProviderState())
        events: List[Dict[str, str]] = []
        if success:
            recovered = state.disconnect_notified
            state.connected = True
            state.consecutive_failures = 0
            state.disconnect_notified = False
            if recovered and self.notify_recovery:
                events.append({"provider": provider, "event": "recovered", "error": ""})
            return events

        state.connected = False
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold and not state.disconnect_notified:
            state.disconnect_notified = True
            events.append({"provider": provider, "event": "disconnected", "error": str(error).strip()})
        return events


def _topic_url(config: Dict[str, Any]) -> str:
    explicit_url = str(config.get("url") or "").strip()
    if explicit_url:
        return explicit_url
    server = str(config.get("server") or "https://ntfy.sh").strip().rstrip("/")
    topic = str(config.get("topic") or "").strip()
    if not topic:
        raise ValueError("ntfy topic is required")
    return f"{server}/{quote(topic, safe='')}"


async def send_ntfy_notification(
    client: httpx.AsyncClient,
    config: Dict[str, Any],
    event: Dict[str, str],
) -> None:
    event_name = event.get("event", "disconnected")
    provider = event.get("provider", "unknown")
    error = event.get("error", "")
    disconnected = event_name == "disconnected"
    title = "AI proxy: provider disconnected" if disconnected else "AI proxy: provider recovered"
    message = f"Provider {provider} is disconnected."
    if not disconnected:
        message = f"Provider {provider} is connected again."
    elif error:
        message += f" Error: {error}"

    headers = {
        "Title": title,
        "Priority": str(config.get("priority", 5 if disconnected else 3)),
        "Tags": str(config.get("tags", "warning,robot_face" if disconnected else "white_check_mark,robot_face")),
    }
    token = str(config.get("token") or os.getenv("AIPROXY_NTFY_TOKEN", "")).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if username and not password:
        raise ValueError("ntfy password is required when username is configured")
    timeout = max(1.0, float(config.get("timeout_seconds", 10)))
    request_kwargs: Dict[str, Any] = {
        "content": message.encode("utf-8"),
        "headers": headers,
        "timeout": timeout,
    }
    if username:
        request_kwargs["auth"] = (username, password)
    response = await client.post(_topic_url(config), **request_kwargs)
    response.raise_for_status()
