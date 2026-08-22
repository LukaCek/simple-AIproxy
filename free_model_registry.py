"""Dynamic free-model provider registry integration.

The registry is public configuration only. Provider credentials stay local and are
resolved from environment variables named by each registry provider's
``api_key_env`` field.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping

import httpx

DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/LukaCek/free-ai-models/main/registry.json"
)
MANAGED_BY = "free-ai-models-registry"
DEFAULT_GROUP = "free-models"
_refresh_task: asyncio.Task[Any] | None = None


def registry_url() -> str:
    return os.getenv("AIPROXY_FREE_MODELS_REGISTRY_URL", DEFAULT_REGISTRY_URL).strip()


def refresh_seconds() -> float:
    raw = os.getenv("AIPROXY_FREE_MODELS_REFRESH_SECONDS", "1800").strip()
    try:
        return max(60.0, float(raw))
    except ValueError:
        return 1800.0


def cache_path(base_dir: Path) -> Path:
    configured = os.getenv("AIPROXY_FREE_MODELS_CACHE_PATH", "").strip()
    if configured:
        return Path(configured)
    return base_dir / "cache" / "free-models-registry.json"


def validate_registry(registry: Any) -> dict[str, Any]:
    if not isinstance(registry, dict):
        raise ValueError("Free-model registry must be a JSON object")
    if registry.get("schema_version") != 1:
        raise ValueError(
            f"Unsupported free-model registry schema_version: {registry.get('schema_version')!r}"
        )
    providers = registry.get("providers")
    if not isinstance(providers, list):
        raise ValueError("Free-model registry providers must be a list")
    return registry


def load_cached_registry(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return validate_registry(data)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def save_cached_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def build_registry_overlay(
    registry: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build in-memory providers and the ``free-models`` group.

    Only ongoing/default-group-eligible providers with a locally configured API
    key enter the group. Trial/prototype entries stay visible in the public
    registry but never silently consume trial credits.
    """

    validate_registry(registry)
    env = environ if environ is not None else os.environ
    providers: list[dict[str, Any]] = []
    ranked_members: list[tuple[int, dict[str, str]]] = []

    for provider in registry.get("providers", []):
        if not isinstance(provider, dict) or provider.get("enabled") is False:
            continue
        free_tier = provider.get("free_tier")
        if not isinstance(free_tier, dict) or not free_tier.get(
            "default_group_eligible", False
        ):
            continue
        key_env = str(provider.get("api_key_env") or "").strip()
        api_key = str(env.get(key_env, "") or "").strip() if key_env else ""
        if not api_key:
            continue
        provider_id = str(provider.get("id") or "").strip()
        endpoint_url = str(provider.get("chat_completions_url") or "").strip()
        if not provider_id or not endpoint_url:
            continue

        enabled_models: list[str] = []
        default_models: list[tuple[int, str]] = []
        for model in provider.get("models", []):
            if not isinstance(model, dict) or model.get("enabled") is False:
                continue
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            enabled_models.append(model_id)
            if model.get("include_in_default_group") is True:
                try:
                    priority = int(model.get("priority", 1000))
                except (TypeError, ValueError):
                    priority = 1000
                default_models.append((priority, model_id))

        if not enabled_models or not default_models:
            continue

        provider_name = f"free-registry-{provider_id}"
        providers.append(
            {
                "name": provider_name,
                "url": endpoint_url,
                "api_key": api_key,
                "description": str(provider.get("name") or provider_id),
                "models": enabled_models,
                "api_mode": str(
                    provider.get("api_mode") or "openai_chat_completions"
                ),
                "managed_by": MANAGED_BY,
                "registry_provider_id": provider_id,
                "registry_api_key_env": key_env,
            }
        )
        for priority, model_id in default_models:
            ranked_members.append(
                (
                    priority,
                    {"provider": provider_name, "model": model_id},
                )
            )

    ranked_members.sort(
        key=lambda item: (item[0], item[1]["provider"], item[1]["model"])
    )
    group = {
        "description": (
            "Automatically managed free-tier pool from LukaCek/free-ai-models. "
            "Only providers with locally configured API keys are active."
        ),
        "strategy": "fallback",
        "members": [member for _, member in ranked_members],
        "managed_by": MANAGED_BY,
        "registry_updated_at": registry.get("updated_at"),
    }
    return providers, group


def apply_registry(
    config_data: dict[str, Any],
    registry: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Apply a registry overlay without persisting secrets into config.yml."""

    providers, group = build_registry_overlay(registry, environ=environ)
    current_providers = config_data.get("providers")
    if not isinstance(current_providers, list):
        current_providers = []
    config_data["providers"] = [
        provider
        for provider in current_providers
        if not (
            isinstance(provider, dict)
            and provider.get("managed_by") == MANAGED_BY
        )
    ] + providers

    groups = config_data.get("groups")
    if not isinstance(groups, dict):
        groups = {}
        config_data["groups"] = groups
    groups[DEFAULT_GROUP] = group
    return config_data


async def fetch_registry(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    response = await client.get(
        url,
        headers={"Accept": "application/json", "User-Agent": "simple-aiproxy/1.0"},
        timeout=5.0,
    )
    response.raise_for_status()
    return validate_registry(response.json())


def _apply_to_impl(impl: Any, registry: dict[str, Any]) -> None:
    with impl.config_lock:
        apply_registry(impl.config_data, registry)


async def refresh_once(impl: Any) -> bool:
    if impl.http_client is None:
        return False
    url = registry_url()
    if not url:
        return False
    registry = await fetch_registry(impl.http_client, url)
    _apply_to_impl(impl, registry)
    save_cached_registry(cache_path(impl.BASE_DIR), registry)
    print(
        "free-models registry refreshed: "
        f"{len(impl.config_data.get('groups', {}).get(DEFAULT_GROUP, {}).get('members', []))} active members"
    )
    return True


async def _refresh_loop(impl: Any) -> None:
    while True:
        await asyncio.sleep(refresh_seconds())
        try:
            await refresh_once(impl)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep last-known-good registry active
            print(f"free-models registry refresh failed: {exc}")


def install(impl: Any) -> None:
    """Attach registry lifecycle hooks to the existing FastAPI application."""

    original_reload_config = impl.reload_config

    def reload_config_with_registry() -> None:
        original_reload_config()
        cached = load_cached_registry(cache_path(impl.BASE_DIR))
        if cached is not None:
            _apply_to_impl(impl, cached)

    impl.reload_config = reload_config_with_registry

    @impl.app.on_event("startup")
    async def free_models_registry_startup() -> None:
        global _refresh_task
        cached = load_cached_registry(cache_path(impl.BASE_DIR))
        if cached is not None:
            _apply_to_impl(impl, cached)
        try:
            await refresh_once(impl)
        except Exception as exc:  # noqa: BLE001 - startup must survive registry outages
            print(f"free-models registry initial refresh failed: {exc}")
        _refresh_task = asyncio.create_task(
            _refresh_loop(impl), name="free-model-registry-refresh"
        )

    @impl.app.on_event("shutdown")
    async def free_models_registry_shutdown() -> None:
        global _refresh_task
        if _refresh_task is not None:
            _refresh_task.cancel()
            try:
                await _refresh_task
            except asyncio.CancelledError:
                pass
            _refresh_task = None
