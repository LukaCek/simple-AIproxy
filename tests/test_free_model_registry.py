from pathlib import Path

import free_model_registry as registry


def sample_registry():
    return {
        "schema_version": 1,
        "updated_at": "2026-08-22",
        "providers": [
            {
                "id": "alpha",
                "name": "Alpha",
                "enabled": True,
                "chat_completions_url": "https://alpha.example/v1/chat/completions",
                "api_mode": "openai_chat_completions",
                "api_key_env": "ALPHA_KEY",
                "free_tier": {
                    "type": "ongoing",
                    "default_group_eligible": True,
                },
                "models": [
                    {
                        "id": "alpha-best",
                        "enabled": True,
                        "include_in_default_group": True,
                        "priority": 20,
                    },
                    {
                        "id": "alpha-extra",
                        "enabled": True,
                        "include_in_default_group": False,
                        "priority": 5,
                    },
                ],
            },
            {
                "id": "beta",
                "name": "Beta",
                "enabled": True,
                "chat_completions_url": "https://beta.example/v1/chat/completions",
                "api_mode": "openai_chat_completions",
                "api_key_env": "BETA_KEY",
                "free_tier": {
                    "type": "ongoing",
                    "default_group_eligible": True,
                },
                "models": [
                    {
                        "id": "beta-best",
                        "enabled": True,
                        "include_in_default_group": True,
                        "priority": 10,
                    }
                ],
            },
            {
                "id": "trial",
                "name": "Trial",
                "enabled": True,
                "chat_completions_url": "https://trial.example/v1/chat/completions",
                "api_mode": "openai_chat_completions",
                "api_key_env": "TRIAL_KEY",
                "free_tier": {
                    "type": "prototype_trial",
                    "default_group_eligible": False,
                },
                "models": [
                    {
                        "id": "trial-model",
                        "enabled": True,
                        "include_in_default_group": True,
                        "priority": 1,
                    }
                ],
            },
        ],
    }


def test_build_overlay_uses_only_keyed_eligible_providers_and_priority_order():
    providers, group = registry.build_registry_overlay(
        sample_registry(),
        environ={"ALPHA_KEY": "alpha-secret", "BETA_KEY": "beta-secret", "TRIAL_KEY": "trial-secret"},
    )

    assert [provider["name"] for provider in providers] == [
        "free-registry-alpha",
        "free-registry-beta",
    ]
    assert providers[0]["models"] == ["alpha-best", "alpha-extra"]
    assert group["strategy"] == "fallback"
    assert group["members"] == [
        {"provider": "free-registry-beta", "model": "beta-best"},
        {"provider": "free-registry-alpha", "model": "alpha-best"},
    ]


def test_build_overlay_skips_provider_without_local_key():
    providers, group = registry.build_registry_overlay(
        sample_registry(), environ={"ALPHA_KEY": "alpha-secret"}
    )

    assert [provider["name"] for provider in providers] == ["free-registry-alpha"]
    assert group["members"] == [
        {"provider": "free-registry-alpha", "model": "alpha-best"}
    ]


def test_apply_registry_replaces_only_managed_providers_and_reserves_free_group():
    config = {
        "providers": [
            {"name": "user-provider", "models": ["user-model"]},
            {
                "name": "free-registry-old",
                "models": ["old"],
                "managed_by": registry.MANAGED_BY,
            },
        ],
        "groups": {
            "user-group": {
                "strategy": "fallback",
                "members": [{"provider": "user-provider", "model": "user-model"}],
            },
            "free-models": {
                "strategy": "fallback",
                "members": [{"provider": "free-registry-old", "model": "old"}],
                "managed_by": registry.MANAGED_BY,
            },
        },
    }

    registry.apply_registry(config, sample_registry(), environ={"BETA_KEY": "beta-secret"})

    assert [provider["name"] for provider in config["providers"]] == [
        "user-provider",
        "free-registry-beta",
    ]
    assert "user-group" in config["groups"]
    assert config["groups"]["free-models"]["members"] == [
        {"provider": "free-registry-beta", "model": "beta-best"}
    ]


def test_cache_round_trip(tmp_path: Path):
    path = tmp_path / "cache" / "registry.json"
    data = sample_registry()

    registry.save_cached_registry(path, data)

    assert registry.load_cached_registry(path) == data


def test_invalid_schema_is_rejected():
    try:
        registry.validate_registry({"schema_version": 2, "providers": []})
    except ValueError as exc:
        assert "schema_version" in str(exc)
    else:
        raise AssertionError("schema version 2 should be rejected")
