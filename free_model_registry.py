"""Dynamic free-model provider registry and local credential management.

The public registry contains provider/model metadata only. Provider credentials are
resolved from environment variables or from the local SQLite database. Registry
providers are injected into the in-memory config and are stripped before any YAML
configuration is persisted, so provider secrets never enter ``config.yml``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/LukaCek/free-ai-models/main/registry.json"
)
MANAGED_BY = "free-ai-models-registry"
DEFAULT_GROUP = "free-models"
_refresh_task: asyncio.Task[Any] | None = None
_active_registry: dict[str, Any] | None = None


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
        raise TypeError("Free-model registry must be a JSON object")
    if registry.get("schema_version") != 1:
        raise ValueError(
            f"Unsupported free-model registry schema_version: {registry.get('schema_version')!r}"
        )
    providers = registry.get("providers")
    if not isinstance(providers, list):
        raise TypeError("Free-model registry providers must be a list")
    return registry


def load_cached_registry(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return validate_registry(data)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def save_cached_registry(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def strip_managed_overlay(config_data: dict[str, Any]) -> dict[str, Any]:
    """Return a persistable config with registry-generated secrets removed."""

    clean = copy.deepcopy(config_data)
    providers = clean.get("providers")
    if isinstance(providers, list):
        clean["providers"] = [
            provider
            for provider in providers
            if not (
                isinstance(provider, dict)
                and provider.get("managed_by") == MANAGED_BY
            )
        ]
    groups = clean.get("groups")
    if isinstance(groups, dict):
        clean["groups"] = {
            name: group
            for name, group in groups.items()
            if not (
                isinstance(group, dict)
                and group.get("managed_by") == MANAGED_BY
            )
        }
    return clean


def mask_secret(secret: str) -> str:
    value = str(secret or "")
    if len(value) <= 8:
        return "••••••••" if value else ""
    return f"{value[:4]}••••{value[-4:]}"


def init_free_provider_key_schema(impl: Any) -> None:
    with impl.get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS FreeProviderKeys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id TEXT NOT NULL,
                name TEXT NOT NULL,
                api_key TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(provider_id, name)
            )
            """
        )
        conn.commit()


def list_stored_key_records(
    impl: Any, *, include_secret: bool = False
) -> list[dict[str, Any]]:
    try:
        with impl.get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, provider_id, name, api_key, enabled, created_at
                FROM FreeProviderKeys
                ORDER BY provider_id, id
                """
            ).fetchall()
    except Exception:
        return []

    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        item["key_display"] = mask_secret(str(item.get("api_key") or ""))
        if not include_secret:
            item.pop("api_key", None)
        result.append(item)
    return result


def upsert_stored_key(impl: Any, provider_id: str, name: str, api_key: str) -> None:
    clean_provider = provider_id.strip()
    clean_name = name.strip()
    clean_key = api_key.strip()
    if not clean_provider:
        raise ValueError("Provider is required")
    if not clean_name:
        raise ValueError("Key name is required")
    if len(clean_name) > 120:
        raise ValueError("Key name is too long")
    if not clean_key:
        raise ValueError("API key is required")
    init_free_provider_key_schema(impl)
    with impl.get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO FreeProviderKeys (provider_id, name, api_key, enabled, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(provider_id, name) DO UPDATE SET
                api_key = excluded.api_key,
                enabled = 1
            """,
            (clean_provider, clean_name, clean_key, impl.datetime.utcnow().isoformat()),
        )
        conn.commit()


def delete_stored_key(impl: Any, key_id: int) -> bool:
    with impl.get_db_connection() as conn:
        cursor = conn.execute("DELETE FROM FreeProviderKeys WHERE id = ?", (key_id,))
        conn.commit()
        return cursor.rowcount > 0


def set_stored_key_enabled(impl: Any, key_id: int, enabled: bool) -> bool:
    with impl.get_db_connection() as conn:
        cursor = conn.execute(
            "UPDATE FreeProviderKeys SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, key_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def _credentials_for_provider(
    provider: dict[str, Any],
    env: Mapping[str, str],
    stored_credentials: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    provider_id = str(provider.get("id") or "").strip()
    credentials: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    key_env = str(provider.get("api_key_env") or "").strip()
    env_key = str(env.get(key_env, "") or "").strip() if key_env else ""
    if env_key:
        seen_keys.add(env_key)
        credentials.append(
            {
                "id": "env",
                "name": key_env,
                "api_key": env_key,
                "source": "environment",
            }
        )

    for record in stored_credentials:
        if str(record.get("provider_id") or "") != provider_id:
            continue
        if record.get("enabled") is False:
            continue
        api_key = str(record.get("api_key") or "").strip()
        if not api_key or api_key in seen_keys:
            continue
        seen_keys.add(api_key)
        credentials.append(
            {
                "id": f"db-{record.get('id')}",
                "name": str(record.get("name") or f"Key {record.get('id')}"),
                "api_key": api_key,
                "source": "database",
            }
        )
    return credentials


def build_registry_overlay(
    registry: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    stored_credentials: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build in-memory free providers and a priority-ordered fallback group.

    Each configured credential becomes a separate endpoint. Credentials for the
    same provider/model are adjacent, so retryable failures such as 429 naturally
    fall through to the next key before the proxy tries a lower-priority model.
    """

    validate_registry(registry)
    env = environ if environ is not None else os.environ
    stored = stored_credentials or []
    providers: list[dict[str, Any]] = []
    ranked_members: list[tuple[int, str, str, int, dict[str, str]]] = []

    for provider in registry.get("providers", []):
        if not isinstance(provider, dict) or provider.get("enabled") is False:
            continue
        free_tier = provider.get("free_tier")
        if not isinstance(free_tier, dict) or not free_tier.get(
            "default_group_eligible", False
        ):
            continue
        provider_id = str(provider.get("id") or "").strip()
        endpoint_url = str(provider.get("chat_completions_url") or "").strip()
        if not provider_id or not endpoint_url:
            continue

        credentials = _credentials_for_provider(provider, env, stored)
        if not credentials:
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

        provider_names: list[str] = []
        for credential_index, credential in enumerate(credentials):
            credential_id = str(credential["id"])
            provider_name = f"free-registry-{provider_id}-{credential_id}"
            provider_names.append(provider_name)
            providers.append(
                {
                    "name": provider_name,
                    "url": endpoint_url,
                    "api_key": credential["api_key"],
                    "description": (
                        f"{provider.get('name') or provider_id} "
                        f"[{credential['name']}]"
                    ),
                    "models": enabled_models,
                    "api_mode": str(
                        provider.get("api_mode") or "openai_chat_completions"
                    ),
                    "managed_by": MANAGED_BY,
                    "registry_provider_id": provider_id,
                    "registry_key_name": credential["name"],
                    "registry_key_source": credential["source"],
                }
            )
            for priority, model_id in default_models:
                ranked_members.append(
                    (
                        priority,
                        provider_id,
                        model_id,
                        credential_index,
                        {"provider": provider_name, "model": model_id},
                    )
                )

    ranked_members.sort(key=lambda item: item[:4])
    group = {
        "description": (
            "Automatically managed free-tier pool from LukaCek/free-ai-models. "
            "Multiple credentials for a provider are chained before moving to "
            "the next free model."
        ),
        "strategy": "fallback",
        "members": [member for *_, member in ranked_members],
        "managed_by": MANAGED_BY,
        "registry_updated_at": registry.get("updated_at"),
    }
    return providers, group


def apply_registry(
    config_data: dict[str, Any],
    registry: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    stored_credentials: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply a registry overlay without persisting secrets into config.yml."""

    providers, group = build_registry_overlay(
        registry,
        environ=environ,
        stored_credentials=stored_credentials,
    )
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
    for group_name, existing_group in list(groups.items()):
        if (
            isinstance(existing_group, dict)
            and existing_group.get("managed_by") == MANAGED_BY
        ):
            groups.pop(group_name, None)
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


def _registry_for_impl(impl: Any) -> dict[str, Any] | None:
    if _active_registry is not None:
        return _active_registry
    return load_cached_registry(cache_path(impl.BASE_DIR))


def _apply_to_impl(impl: Any, registry: dict[str, Any]) -> None:
    stored = list_stored_key_records(impl, include_secret=True)
    with impl.config_lock:
        apply_registry(
            impl.config_data,
            registry,
            stored_credentials=stored,
        )


def provider_statuses(impl: Any, registry: dict[str, Any] | None) -> list[dict[str, Any]]:
    if registry is None:
        return []
    stored = list_stored_key_records(impl, include_secret=False)
    statuses: list[dict[str, Any]] = []
    for provider in registry.get("providers", []):
        if not isinstance(provider, dict) or provider.get("enabled") is False:
            continue
        provider_id = str(provider.get("id") or "").strip()
        if not provider_id:
            continue
        free_tier = provider.get("free_tier") if isinstance(provider.get("free_tier"), dict) else {}
        eligible = bool(free_tier.get("default_group_eligible", False))
        key_env = str(provider.get("api_key_env") or "").strip()
        env_configured = bool(str(os.environ.get(key_env, "") or "").strip()) if key_env else False
        stored_for_provider = [
            key
            for key in stored
            if key.get("provider_id") == provider_id and key.get("enabled")
        ]
        default_models = [
            str(model.get("id"))
            for model in provider.get("models", [])
            if isinstance(model, dict)
            and model.get("enabled") is not False
            and model.get("include_in_default_group") is True
            and model.get("id")
        ]
        configured_count = len(stored_for_provider) + (1 if env_configured else 0)
        statuses.append(
            {
                "id": provider_id,
                "name": str(provider.get("name") or provider_id),
                "eligible": eligible,
                "configured": configured_count > 0,
                "configured_count": configured_count,
                "stored_count": len(stored_for_provider),
                "env_configured": env_configured,
                "api_key_env": key_env,
                "models": default_models,
                "signup_url": str(provider.get("signup_url") or ""),
                "free_tier_type": str(free_tier.get("type") or "unknown"),
                "free_tier_summary": str(free_tier.get("summary") or ""),
            }
        )
    return statuses


def stored_keys_for_ui(impl: Any, registry: dict[str, Any] | None) -> list[dict[str, Any]]:
    provider_names: dict[str, str] = {}
    if registry is not None:
        for provider in registry.get("providers", []):
            if isinstance(provider, dict) and provider.get("id"):
                provider_names[str(provider["id"])] = str(
                    provider.get("name") or provider["id"]
                )
    keys = list_stored_key_records(impl, include_secret=False)
    for key in keys:
        provider_id = str(key.get("provider_id") or "")
        key["provider_name"] = provider_names.get(provider_id, provider_id)
    return keys


def _find_registry_provider(
    registry: dict[str, Any] | None, provider_id: str
) -> dict[str, Any] | None:
    if registry is None:
        return None
    for provider in registry.get("providers", []):
        if isinstance(provider, dict) and str(provider.get("id") or "") == provider_id:
            return provider
    return None


async def refresh_once(impl: Any) -> bool:
    global _active_registry
    if impl.http_client is None:
        return False
    url = registry_url()
    if not url:
        return False
    registry = await fetch_registry(impl.http_client, url)
    _active_registry = registry
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


def _remove_existing_config_get_route(impl: Any) -> None:
    retained = []
    for route in impl.app.router.routes:
        methods = getattr(route, "methods", set()) or set()
        if getattr(route, "path", None) == "/admin/config" and "GET" in methods:
            continue
        retained.append(route)
    impl.app.router.routes[:] = retained


def install(impl: Any) -> None:
    """Attach registry lifecycle, safe persistence, and credential UI routes."""

    original_reload_config = impl.reload_config
    original_save_config = impl.save_config

    def save_config_without_registry(data: dict[str, Any]) -> None:
        clean = strip_managed_overlay(data)
        original_save_config(clean)
        registry = _registry_for_impl(impl)
        if registry is not None:
            _apply_to_impl(impl, registry)

    def reload_config_with_registry() -> None:
        original_reload_config()
        registry = _registry_for_impl(impl)
        if registry is not None:
            _apply_to_impl(impl, registry)

    impl.save_config = save_config_without_registry
    impl.reload_config = reload_config_with_registry

    _remove_existing_config_get_route(impl)

    @impl.app.get(
        "/admin/config",
        response_class=HTMLResponse,
        dependencies=[Depends(impl.verify_admin)],
    )
    def admin_config_with_free_keys(
        request: Request, message: str | None = None
    ) -> Any:
        registry = _registry_for_impl(impl)
        with impl.config_lock:
            yaml_text = impl.yaml.safe_dump(
                strip_managed_overlay(impl.config_data), sort_keys=False
            )
        statuses = provider_statuses(impl, registry)
        eligible = [item for item in statuses if item["eligible"]]
        ready_count = sum(1 for item in eligible if item["configured"])
        return impl.templates.TemplateResponse(
            request=request,
            name="config.html",
            context={
                "yaml_text": yaml_text,
                "message": message,
                "free_provider_statuses": statuses,
                "free_provider_keys": stored_keys_for_ui(impl, registry),
                "free_registry_updated_at": registry.get("updated_at") if registry else None,
                "free_registry_available": registry is not None,
                "free_ready_count": ready_count,
                "free_eligible_count": len(eligible),
            },
        )

    @impl.app.post(
        "/admin/config/free-provider-keys",
        dependencies=[Depends(impl.verify_admin)],
    )
    async def admin_free_provider_key_add(
        provider_id: str = Form(...),
        key_name: str = Form(...),
        api_key: str = Form(...),
    ) -> RedirectResponse:
        registry = _registry_for_impl(impl)
        provider = _find_registry_provider(registry, provider_id.strip())
        if provider is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unknown free-model registry provider",
            )
        free_tier = provider.get("free_tier")
        if not isinstance(free_tier, dict) or not free_tier.get(
            "default_group_eligible", False
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This provider is not eligible for the default free-models pool",
            )
        try:
            upsert_stored_key(impl, provider_id, key_name, api_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        if registry is not None:
            _apply_to_impl(impl, registry)
        message = quote(f"Saved {provider.get('name') or provider_id} key '{key_name.strip()}'")
        return RedirectResponse(
            url=f"/admin/config?message={message}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @impl.app.post(
        "/admin/config/free-provider-keys/{key_id}/toggle",
        dependencies=[Depends(impl.verify_admin)],
    )
    async def admin_free_provider_key_toggle(
        key_id: int, enabled: str = Form(...)
    ) -> RedirectResponse:
        if not set_stored_key_enabled(impl, key_id, enabled == "1"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
        registry = _registry_for_impl(impl)
        if registry is not None:
            _apply_to_impl(impl, registry)
        return RedirectResponse(
            url="/admin/config?message=Updated+free+provider+key",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @impl.app.post(
        "/admin/config/free-provider-keys/{key_id}/delete",
        dependencies=[Depends(impl.verify_admin)],
    )
    async def admin_free_provider_key_delete(key_id: int) -> RedirectResponse:
        if not delete_stored_key(impl, key_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Key not found")
        registry = _registry_for_impl(impl)
        if registry is not None:
            _apply_to_impl(impl, registry)
        return RedirectResponse(
            url="/admin/config?message=Deleted+free+provider+key",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @impl.app.on_event("startup")
    async def free_models_registry_startup() -> None:
        global _active_registry, _refresh_task
        init_free_provider_key_schema(impl)

        # Remove any registry-generated entries that may have been persisted by an
        # older version before the safe-save wrapper existed.
        with impl.config_lock:
            clean = strip_managed_overlay(impl.config_data)
            if clean != impl.config_data:
                original_save_config(clean)

        cached = load_cached_registry(cache_path(impl.BASE_DIR))
        if cached is not None:
            _active_registry = cached
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
