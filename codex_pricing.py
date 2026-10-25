"""Codex credit comparison from the official ChatGPT Work/Codex rate card.

Prices are **credits per million tokens**, not fixed per-request charges.
A verified fallback snapshot keeps the comparison useful if the public pricing
page is unreachable. New model IDs only get a price when the rate card explicitly
contains that model; never infer a price from a model family or a plan name.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

RATE_CARD_URL = (
    "https://help.openai.com/en/articles/"
    "11481834-chatgpt-rate-card-business-enterpriseedu-credit-based-pricing"
)
PERSONAL_PLAN_URL = (
    "https://help.openai.com/en/articles/"
    "12642688-using-credits-for-flexible-usage-in-chatgpt-personal-plans"
)
REFRESH_SECONDS = max(3600, int(os.getenv("CODEX_PRICE_REFRESH_SECONDS", "86400")))
# Verified against the ChatGPT Work and Codex STANDARD token-rate table,
# October 25, 2026. It is a fallback, not a claim of live billing information.
VERIFIED_SNAPSHOT = {
    "gpt-6.1-sol": (50, 2.5, 250),
    "gpt-6-astra": (250, 25, 1250),
    "gpt-6-astra-law": (312.5, 31.25, 1562.5),
    "gpt-6-sol": (50, 5, 250),
    "gpt-6-luna": (2.5, .25, 12.5),
    "gpt-5.6-sol": (100, 10, 500),
    "gpt-5.6-terra": (50, 5, 300),
    "gpt-5.6-luna": (5, .5, 30),
    "gpt-rosalind-research": (125, 12.5, 625),
    "daybreak-blue": (100, 10, 500),
    "daybreak-red": (312.5, 31.25, 1875),
    "gpt-5.3-codex": (43.75, 4.375, 350),
    "gpt-5.2": (43.75, 4.375, 350),
    "gpt-image-2-image": (200, 50, 750),
    "gpt-image-2-text": (125, 31.25, 250),
}
DEFAULT_INPUT_TOKENS = 10000
DEFAULT_CACHED_INPUT_TOKENS = 0
DEFAULT_OUTPUT_TOKENS = 2000
_current_prices: dict[str, dict[str, float]] = {}
_checked_at: float | None = None
_updated_at: float | None = None
_error: str | None = None
_task: asyncio.Task | None = None
_impl: Any = None


def _record(values: tuple[float, float, float] | list[float]) -> dict[str, float]:
    return {"input": float(values[0]), "cached_input": float(values[1]), "output": float(values[2])}


def snapshot_prices() -> dict[str, dict[str, float]]:
    return {key: _record(values) for key, values in VERIFIED_SNAPSHOT.items()}


class _Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._level = 0
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._suppressed += 1
        if tag == "table":
            self._level += 1
            if self._level == 1:
                self._table = []
        elif self._level == 1 and tag == "tr":
            self._row = []
        elif self._level == 1 and tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._suppressed:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._suppressed = max(0, self._suppressed - 1)
        if tag in {"th", "td"} and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None and self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._level:
            if self._level == 1 and self._table is not None:
                self.tables.append(self._table)
                self._table = None
            self._level -= 1


def _slug(label: str) -> str:
    """Normalize rate-card model display names, not arbitrary proxy aliases."""
    label = label.strip().replace("–", "-").replace("—", "-")
    label = label.replace("*", "")
    label = re.sub(r"\s*\([^)]*\)", "", label)
    label = re.sub(r"[^a-zA-Z0-9.]+", "-", label).lower().strip("-")
    return label


def _numeric(cell: str) -> float:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", cell)
    if not match:
        raise ValueError(f"Missing numeric credit rate: {cell[:80]}")
    value = float(match.group(0).replace(",", ""))
    if not 0 < value <= 100000:
        raise ValueError("Invalid credit rate")
    return value


def parse_rate_card_html(body: str) -> dict[str, dict[str, float]]:
    parser = _Tables()
    parser.feed(body)
    for rows in parser.tables:
        if not rows:
            continue
        header = [c.lower() for c in rows[0]]
        # Only the token-based Work/Codex table has all four named columns.
        # Avoid the different per-message and legacy rate cards.
        if (len(header) < 4 or "model" not in header[0] or
                "input tokens" not in header[1] or
                "cached input tokens" not in header[2] or
                "output tokens" not in header[3] or
                "credit" not in " ".join(header)):
            continue
        parsed: dict[str, dict[str, float]] = {}
        for row in rows[1:]:
            if len(row) < 4:
                continue
            slug = _slug(row[0])
            if not slug or slug in parsed:
                continue
            try:
                parsed[slug] = _record(
                    (_numeric(row[1]), _numeric(row[2]), _numeric(row[3]))
                )
            except ValueError:
                continue
        # Refuse to replace trusted pricing with a broken/partial page.
        if len(parsed) >= 5 and {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"} <= parsed.keys():
            return parsed
    raise ValueError("Official Codex standard token-rate table could not be parsed")


def cache_path() -> Path:
    if _impl is None:
        return Path("cache/codex-pricing.json")
    return Path(_impl.BASE_DIR) / "cache" / "codex-pricing.json"


def load_cache() -> None:
    global _current_prices, _checked_at, _updated_at, _error
    _current_prices = snapshot_prices()
    _updated_at = None
    _checked_at = None
    _error = "Using published 2026-10-25 fallback; live rate card not verified"
    try:
        data = json.loads(cache_path().read_text())
        prices = data.get("prices") if isinstance(data, dict) else None
        if not isinstance(prices, dict) or len(prices) < 5:
            return
        normalized = {}
        for name, rate in prices.items():
            if isinstance(rate, dict):
                normalized[name] = _record(
                    [float(rate[field]) for field in ("input", "cached_input", "output")]
                )
        if len(normalized) < 5:
            return
        _current_prices = normalized
        _updated_at = float(data["updated_at"])
        _checked_at = float(data.get("checked_at") or _updated_at)
        _error = None
    except (OSError, ValueError, KeyError, TypeError):
        pass


async def refresh_once() -> bool:
    global _current_prices, _checked_at, _updated_at, _error
    if _impl is None or _impl.http_client is None:
        return False
    _checked_at = time.time()
    try:
        response = await _impl.http_client.get(
            RATE_CARD_URL, headers={"Accept": "text/html"}, timeout=25.0
        )
        response.raise_for_status()
        prices = parse_rate_card_html(response.text)
        _current_prices = prices
        _updated_at = time.time()
        _error = None
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "prices": prices, "updated_at": _updated_at,
            "checked_at": _checked_at, "source": RATE_CARD_URL,
        }), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as exc:
        _error = f"{type(exc).__name__}: {str(exc)[:160]}"
        return False


def price_for_model(model: str) -> dict[str, float] | None:
    return dict(_current_prices[model]) if model in _current_prices else None


def public_meta() -> dict[str, Any]:
    return {
        "source_url": RATE_CARD_URL,
        "personal_plan_url": PERSONAL_PLAN_URL,
        "checked_at": _checked_at,
        "updated_at": _updated_at,
        "fallback_snapshot_at": "2026-10-25" if _updated_at is None else None,
        "error": _error,
        "unit": "credits_per_1m_tokens",
        "mode": "standard",
        "example": {
            "input_tokens": DEFAULT_INPUT_TOKENS,
            "cached_input_tokens": DEFAULT_CACHED_INPUT_TOKENS,
            "output_tokens": DEFAULT_OUTPUT_TOKENS,
        },
    }


def estimate_credits(
    rates: dict[str, float], input_tokens: int, cached_input_tokens: int,
    output_tokens: int,
) -> float:
    """Input includes cached tokens; charge only noncached at the input rate."""
    if not all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 100000000
               for x in (input_tokens, cached_input_tokens, output_tokens)):
        raise ValueError("Token counts must be nonnegative integers")
    if cached_input_tokens > input_tokens:
        raise ValueError("Cached input cannot exceed total input")
    return (
        ((input_tokens - cached_input_tokens) / 1e6) * rates["input"] +
        (cached_input_tokens / 1e6) * rates["cached_input"] +
        (output_tokens / 1e6) * rates["output"]
    )


async def _refresh_loop() -> None:
    while True:
        await refresh_once()
        await asyncio.sleep(REFRESH_SECONDS)


def install(impl: Any) -> None:
    global _impl
    _impl = impl
    load_cache()

    @impl.app.on_event("startup")
    async def codex_pricing_startup() -> None:
        global _task
        _task = asyncio.create_task(_refresh_loop(), name="codex-pricing-refresh")

    @impl.app.on_event("shutdown")
    async def codex_pricing_shutdown() -> None:
        global _task
        if _task is not None:
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass
            _task = None
