"""Regression coverage for account-scoped Codex models and quota routing."""
import asyncio
import json
import time

import httpx
import pytest
from fastapi import HTTPException

import main
import codex_registry as reg


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.db")
    main.init_database()
    reg._impl = main
    reg._original_headers = main.build_provider_headers
    reg.init_schema()
    monkeypatch.setattr(main, "http_client", None)
    with main.config_lock:
        main.config_data = {
            "providers": [
                {"name": "pro", "api_mode": "codex_responses", "oauth": True,
                 "url": "https://chatgpt.com/backend-api/codex",
                 "access_token": "test-pro", "models": ["gpt-5.6-sol"]},
                {"name": "basic", "api_mode": "codex_responses", "oauth": True,
                 "url": "https://chatgpt.com/backend-api/codex",
                 "access_token": "test-basic", "models": ["gpt-5.6-sol"]},
            ],
            "groups": {
                "codex": {"strategy": "round_robin", "members": [
                    {"provider": "pro", "model": "gpt-5.6-sol"},
                    {"provider": "basic", "model": "gpt-5.6-sol"},
                ]},
                "hermes-main": {"members": [{"group": "codex"}]},
                "gpt-5.6-sol": {"members": [
                    {"provider": "pro", "model": "gpt-5.6-sol"},
                    {"provider": "basic", "model": "gpt-5.6-sol"},
                ]},
            },
        }
    yield reg
    main.config_data = {}


def put_models(registry, provider, names):
    now = time.time()
    with registry._conn() as db:
        db.execute(
            "INSERT INTO CodexCatalogs(provider,checked_at,updated_at) VALUES (?,?,?)",
            (provider, now, now),
        )
        db.executemany(
            "INSERT INTO CodexModels(provider,slug,display_name,position,is_default)"
            "VALUES(?,?,?,?,?)",
            [(provider, name, name, i, int(i == 0)) for i, name in enumerate(names)],
        )


def test_parse_discovery_catalog_strict_and_forward_compatible():
    models = reg.parse_models({"models": [
        {"slug": "gpt-7", "display_name": "GPT 7", "is_default": True},
        {"slug": "gpt-7"}, {"slug": "hidden", "visibility": "hidden"},
        {"slug": ""}, {"other": "invalid"},
    ]})
    assert models == [{"slug": "gpt-7", "display_name": "GPT 7", "is_default": True}]
    with pytest.raises(ValueError):
        reg.parse_models({"error": "not authenticated"})


def test_only_supported_accounts_are_routed(registry):
    put_models(registry, "pro", ["gpt-5.6-sol", "gpt-7"])
    put_models(registry, "basic", ["gpt-6-luna"])
    assert [p["name"] for p in reg.resolve_requested_model("gpt-5.6-sol")] == ["pro"]
    assert [p["name"] for p in reg.resolve_requested_model("gpt-7")] == ["pro"]
    assert {p["name"] for p in reg.resolve_requested_model("hermes-main")} == {"pro", "basic"}
    assert {p["model"] for p in reg.resolve_requested_model("hermes-main")} == {"gpt-5.6-sol", "gpt-6-luna"}


def test_model_not_found_skips_account_but_other_account_can_work(registry):
    put_models(registry, "pro", ["gpt-5.6-sol"])
    put_models(registry, "basic", ["gpt-5.6-sol"])
    endpoint = reg.resolve_requested_model("gpt-5.6-sol")[0]
    assert reg.note_error(
        endpoint, 400, json.dumps({"error": {"code": "model_not_found"}})
    )
    remaining = reg.resolve_requested_model("gpt-5.6-sol")
    assert len(remaining) == 1
    assert remaining[0]["name"] != endpoint["name"]
    assert not reg.note_error(remaining[0], 400, '{"error":{"code":"invalid_request_error"}}')


def test_429_resets_at_persisted_until_real_reset(registry):
    put_models(registry, "pro", ["gpt-5.6-sol", "gpt-7"])
    put_models(registry, "basic", ["gpt-5.6-sol"])
    until = time.time() + 7200
    endpoint = next(p for p in reg.resolve_requested_model("gpt-5.6-sol") if p["name"] == "pro")
    assert not reg.note_error(
        endpoint, 429, json.dumps({
            "error": {"type": "usage_limit_reached", "resets_at": until}
        })
    )
    assert reg.block_until("pro", "gpt-7") >= until - 1
    assert [p["name"] for p in reg.resolve_requested_model("gpt-5.6-sol")] == ["basic"]
    with reg._conn() as db:
        db.execute("UPDATE CodexBlocks SET until_at=? WHERE provider=?", (time.time() - 1, "pro"))
    assert {p["name"] for p in reg.resolve_requested_model("gpt-5.6-sol")} == {"pro", "basic"}


def test_no_guessed_models_without_catalog(registry):
    with pytest.raises(HTTPException) as error:
        reg.resolve_requested_model("gpt-5.6-sol")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_catalog_refresh_and_etag_are_account_scoped(registry, monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        async def get(self, url, *, headers, params, timeout):
            self.calls.append((url, headers, params))
            account = headers.get("Authorization")
            names = ["gpt-7", "gpt-6-luna"] if account == "Bearer test-pro" else ["gpt-6-luna"]
            request = httpx.Request("GET", url)
            return httpx.Response(200, request=request,
                json={"models": [{"slug": m} for m in names]},
                headers={"ETag": account[-4:]})

    client = Client()
    monkeypatch.setattr(main, "http_client", client)
    await asyncio.gather(reg.refresh_catalog("pro"), reg.refresh_catalog("basic"))
    assert [m["slug"] for m in reg.catalog_models("pro")] == ["gpt-7", "gpt-6-luna"]
    assert [m["slug"] for m in reg.catalog_models("basic")] == ["gpt-6-luna"]
    await reg.refresh_catalog("basic")
    assert client.calls[-1][1]["If-None-Match"] == "asic"
    assert client.calls[-1][2]["client_version"] == reg.CLIENT_VERSION
