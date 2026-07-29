"""Runtime extensions for selectable Codex models.

This module imports the existing application and then patches the Codex profile
helpers before FastAPI startup loads config.yml. Keeping this separate avoids
making the compatibility layer in main.py even larger.
"""

from __future__ import annotations

import os
from typing import Any

import main as _impl

_DEFAULT_CODEX_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
)

# Keep stable references to the real implementations. Tests reload this module,
# and without storing these originals on main_impl itself a reload would capture
# our already-patched wrappers and recurse forever.
if not hasattr(_impl, "_codex_original_get_default_codex_provider"):
    _impl._codex_original_get_default_codex_provider = (
        _impl.get_default_codex_provider
    )
if not hasattr(_impl, "_codex_original_normalize_config_schema"):
    _impl._codex_original_normalize_config_schema = _impl.normalize_config_schema
if not hasattr(_impl, "_codex_original_upsert_codex_profile"):
    _impl._codex_original_upsert_codex_profile = _impl.upsert_codex_profile

_original_get_default_codex_provider = (
    _impl._codex_original_get_default_codex_provider
)
_original_normalize_config_schema = _impl._codex_original_normalize_config_schema
_original_upsert_codex_profile = _impl._codex_original_upsert_codex_profile


def configured_codex_models() -> list[str]:
    """Return selectable Codex model IDs with the preferred default first."""
    configured = os.getenv("CODEX_MODELS", "").strip()
    models = [
        part.strip()
        for part in configured.replace(",", "\n").splitlines()
        if part.strip()
    ]
    if not models:
        models = list(_DEFAULT_CODEX_MODELS)

    default_model = os.getenv("CODEX_DEFAULT_MODEL", "gpt-5.6-sol").strip()
    if default_model:
        models = [default_model, *models]

    deduplicated: list[str] = []
    for model in models:
        if model not in deduplicated:
            deduplicated.append(model)
    return deduplicated


def get_default_codex_provider() -> dict[str, Any]:
    provider = _original_get_default_codex_provider()
    provider["models"] = configured_codex_models()
    return provider


def _ensure_member(
    groups: dict[str, Any],
    group_name: str,
    provider_name: str,
    model_name: str,
    description: str,
) -> None:
    group = groups.setdefault(
        group_name,
        {
            "description": description,
            "strategy": "round_robin",
            "members": [],
        },
    )
    if not isinstance(group, dict):
        group = {
            "description": description,
            "strategy": "round_robin",
            "members": [],
        }
        groups[group_name] = group
    group.setdefault("description", description)
    group.setdefault("strategy", "round_robin")
    members = group.setdefault("members", [])
    if not isinstance(members, list):
        members = []
        group["members"] = members
    member = {"provider": provider_name, "model": model_name}
    if member not in members:
        members.append(member)


def normalize_config_schema(data: dict[str, Any]) -> dict[str, Any]:
    """Expose every configured Codex model directly and through model pools."""
    normalized = _original_normalize_config_schema(data)
    providers = normalized.get("providers", [])
    groups = normalized.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        normalized["groups"] = groups

    preferred_models = configured_codex_models()
    default_model = preferred_models[0]

    for provider in providers if isinstance(providers, list) else []:
        if not isinstance(provider, dict):
            continue
        if provider.get("api_mode") != "codex_responses":
            continue

        existing_models = [
            str(model).strip()
            for model in provider.get("models", [])
            if str(model).strip()
        ]
        merged_models: list[str] = []
        for model in [*preferred_models, *existing_models]:
            if model not in merged_models:
                merged_models.append(model)
        provider["models"] = merged_models

        provider_name = str(provider.get("name") or "").strip()
        if not provider_name:
            continue
        for model in merged_models:
            _ensure_member(
                groups,
                model,
                provider_name,
                model,
                f"Codex pool for {model}",
            )
        _ensure_member(
            groups,
            "codex",
            provider_name,
            default_model,
            f"Default Codex pool ({default_model})",
        )

    return normalized


def upsert_codex_profile(
    name: str,
    access_token: str = "",
    refresh_token: str = "",
    description: str = "",
) -> None:
    """Create/update a profile and expose all selectable Codex model IDs."""
    _original_upsert_codex_profile(
        name=name,
        access_token=access_token,
        refresh_token=refresh_token,
        description=description,
    )
    models = configured_codex_models()
    for model in models:
        _impl.ensure_group_member(
            model,
            name.strip(),
            model,
            description=f"Codex pool for {model}",
            strategy="round_robin",
        )
    _impl.ensure_group_member(
        "codex",
        name.strip(),
        models[0],
        description=f"Default Codex pool ({models[0]})",
        strategy="round_robin",
    )


# Route handlers and startup functions resolve these names from main_impl globals.
_impl.get_default_codex_provider = get_default_codex_provider
_impl.normalize_config_schema = normalize_config_schema
_impl.upsert_codex_profile = upsert_codex_profile

app = _impl.app
