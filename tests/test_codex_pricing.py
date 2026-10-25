"""Official Codex pricing parsing, estimates, cache and admin UI regression tests."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import codex_pricing as pricing


def rate_card_html(extra_row: str = "") -> str:
    return """
    <html><main>
    <table><tr><th>Model</th><th>Unit</th><th>Credits</th></tr>
    <tr><td>GPT-5.6 Sol</td><td>1 message</td><td>10 credits</td></tr></table>
    <h2>ChatGPT Work and Codex</h2>
    <table>
      <thead><tr>
        <th>Model</th><th>Input tokens (credits per 1M)</th>
        <th>Cached input tokens (credits per 1M)</th>
        <th>Output tokens (credits per 1M)</th>
      </tr></thead>
      <tbody>
      <tr><td>GPT-6.1 Sol</td><td>50 credits</td><td>2.5 credits</td><td>250 credits</td></tr>
      <tr><td>GPT-5.6 Sol</td><td>100 credits</td><td>10 credits</td><td>500 credits</td></tr>
      <tr><td>GPT-5.6 Terra</td><td>50 credits</td><td>5 credits</td><td>300 credits</td></tr>
      <tr><td>GPT-5.6 Luna</td><td>5 credits</td><td>0.5 credits</td><td>30 credits</td></tr>
      <tr><td>GPT-6 Astra</td><td>250 credits</td><td>25 credits</td><td>1,250 credits</td></tr>
      """ + extra_row + """
      </tbody>
    </table></main></html>
    """


def test_rate_card_parser_uses_codex_token_table_not_chat_message_rates():
    rates = pricing.parse_rate_card_html(rate_card_html())
    assert rates["gpt-5.6-sol"] == {"input": 100, "cached_input": 10, "output": 500}
    assert rates["gpt-6.1-sol"] == {"input": 50, "cached_input": 2.5, "output": 250}
    assert rates["gpt-6-astra"]["output"] == 1250


def test_new_officially_listed_model_automatically_gets_rate():
    rates = pricing.parse_rate_card_html(
        rate_card_html("<tr><td>GPT-7 Sol</td><td>60 credits</td>"
                       "<td>6 credits</td><td>320 credits</td></tr>")
    )
    assert rates["gpt-7-sol"] == {"input": 60, "cached_input": 6, "output": 320}


def test_invalid_or_non_codex_table_is_rejected():
    with pytest.raises(ValueError):
        pricing.parse_rate_card_html("<table><tr><th>Model</th><th>Unit</th></tr></table>")
    with pytest.raises(ValueError):
        pricing.parse_rate_card_html('<h1>Rate card</h1><div>API unavailable</div>')


def test_credit_estimate_counts_cached_as_part_of_input():
    sol = pricing.snapshot_prices()["gpt-5.6-sol"]
    assert pricing.estimate_credits(sol, 10000, 0, 2000) == 2.0
    assert pricing.estimate_credits(sol, 10000, 4000, 2000) == pytest.approx(1.64)
    luna = pricing.snapshot_prices()["gpt-5.6-luna"]
    assert pricing.estimate_credits(luna, 10000, 0, 2000) == pytest.approx(0.11)
    with pytest.raises(ValueError):
        pricing.estimate_credits(sol, 1000, 2000, 0)


@pytest.mark.asyncio
async def test_refresh_persists_official_rates_without_tokens(tmp_path, monkeypatch):
    class Client:
        async def get(self, url, *, headers, timeout):
            assert url == pricing.RATE_CARD_URL
            assert "Authorization" not in headers
            return httpx.Response(200, text=rate_card_html())

    monkeypatch.setattr(
        pricing, "_impl", SimpleNamespace(BASE_DIR=tmp_path, http_client=Client())
    )
    monkeypatch.setattr(pricing, "_current_prices", pricing.snapshot_prices())
    assert await pricing.refresh_once()
    data = json.loads((tmp_path / "cache" / "codex-pricing.json").read_text())
    assert data["prices"]["gpt-6.1-sol"]["input"] == 50
    assert pricing.price_for_model("gpt-6.1-sol")["output"] == 250
    assert pricing.price_for_model("gpt-99-not-published") is None


@pytest.mark.asyncio
async def test_failed_update_preserves_verified_prices(tmp_path, monkeypatch):
    class Client:
        async def get(self, *args, **kwargs):
            return httpx.Response(503, request=httpx.Request("GET", pricing.RATE_CARD_URL))

    initial = pricing.snapshot_prices()
    monkeypatch.setattr(
        pricing, "_impl", SimpleNamespace(BASE_DIR=tmp_path, http_client=Client())
    )
    monkeypatch.setattr(pricing, "_current_prices", initial)
    assert not await pricing.refresh_once()
    assert pricing.price_for_model("gpt-5.6-luna") == initial["gpt-5.6-luna"]


def test_codex_usage_template_contains_comparison_and_valid_js():
    template = Path(__file__).resolve().parents[1] / "templates" / "codex_usage.html"
    text = template.read_text()
    assert "Compare Codex credit costs" in text
    assert "pricing-input" in text
    assert "pricing-cached" in text
    assert "pricing-output" in text
    assert "Credits / 1M tokens" in text
    assert "Rate unavailable" in text
    match = re.search(r"<script>(.*?)</script>", text, flags=re.S)
    assert match
    if shutil.which("node"):
        result = subprocess.run(["node", "--check"], input=match.group(1),
                                text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
