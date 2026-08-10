import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_monitor import ProviderDisconnectMonitor, send_ntfy_notification
import main


def test_disconnect_alert_is_sent_once_after_threshold_and_recovery():
    events = []
    monitor = ProviderDisconnectMonitor(failure_threshold=2, notify_recovery=True)

    assert monitor.record("codex-a", True, "") == []
    assert monitor.record("codex-a", False, "timeout") == []
    events += monitor.record("codex-a", False, "timeout again")
    assert events == [
        {
            "provider": "codex-a",
            "event": "disconnected",
            "error": "timeout again",
        }
    ]
    assert monitor.record("codex-a", False, "still down") == []
    assert monitor.record("codex-a", True, "") == [
        {"provider": "codex-a", "event": "recovered", "error": ""}
    ]


def test_initial_failure_can_alert_after_threshold():
    monitor = ProviderDisconnectMonitor(failure_threshold=1, notify_recovery=False)
    assert monitor.record("ollama", False, "connection refused")[0]["event"] == "disconnected"


def test_ntfy_notification_request_shape():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = (await request.aread()).decode()
        return httpx.Response(200, json={"id": "test"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await send_ntfy_notification(
                client,
                {
                    "server": "https://ntfy.example",
                    "topic": "aiproxy-alerts",
                    "token": "secret-token",
                    "priority": 5,
                },
                {"provider": "codex-a", "event": "disconnected", "error": "token expired"},
            )

    asyncio.run(run())
    assert captured["url"] == "https://ntfy.example/aiproxy-alerts"
    assert captured["headers"]["authorization"] == "Bearer secret-token"
    assert captured["headers"]["title"] == "AI proxy: provider disconnected"
    assert captured["headers"]["priority"] == "5"
    assert "codex-a" in captured["body"]
    assert "token expired" in captured["body"]


def test_normalized_config_enables_default_cekluka_ntfy_without_credentials():
    config = main.normalize_config_schema({"providers": [], "groups": {}})
    ntfy = config["notifications"]["ntfy"]
    assert ntfy["enabled"] is True
    assert ntfy["url"] == "https://ntfy.cekluka.com/aiproxy"
    assert ntfy["username"] == ""
    assert ntfy["password"] == ""


def test_monitor_check_publishes_disconnect_event(monkeypatch):
    published = []
    monitor = ProviderDisconnectMonitor(failure_threshold=1)
    monkeypatch.setattr(
        main,
        "config_data",
        {"notifications": {"ntfy": {"enabled": True, "url": "https://ntfy.test/topic"}}},
    )

    async def fake_test_all(prompt):
        return {
            "results": [
                {"provider": "codex-a", "success": False, "response": "offline"}
            ]
        }

    async def fake_send(client, config, event):
        published.append(event)

    async def run():
        async with httpx.AsyncClient() as client:
            monkeypatch.setattr(main, "http_client", client)
            monkeypatch.setattr(main, "test_all_provider_models", fake_test_all)
            monkeypatch.setattr(main, "send_ntfy_notification", fake_send)
            return await main.run_provider_monitor_once(monitor)

    events = asyncio.run(run())
    assert events[0]["event"] == "disconnected"
    assert published == events


def test_ntfy_topic_url_is_supported():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await send_ntfy_notification(
                client,
                {"url": "https://ntfy.sh/my-private-topic"},
                {"provider": "ollama", "event": "recovered", "error": ""},
            )

    asyncio.run(run())
    assert captured["url"] == "https://ntfy.sh/my-private-topic"
