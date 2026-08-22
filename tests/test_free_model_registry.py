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


def test_build_overlay_uses_env_and_multiple_stored_keys_in_priority_order():
    stored = [
        {
            "id": 11,
            "provider_id": "alpha",
            "name": "Luka",
            "api_key": "alpha-luka",
            "enabled": True,
        },
        {
            "id": 12,
            "provider_id": "alpha",
            "name": "Brother",
            "api_key": "alpha-brother",
            "enabled": True,
        },
    ]
    providers, group = registry.build_registry_overlay(
        sample_registry(),
        environ={
            "ALPHA_KEY": "alpha-env",
            "BETA_KEY": "beta-env",
            "TRIAL_KEY": "trial-secret",
        },
        stored_credentials=stored,
    )

    assert [provider["name"] for provider in providers] == [
        "free-registry-alpha-env",
        "free-registry-alpha-db-11",
        "free-registry-alpha-db-12",
        "free-registry-beta-env",
    ]
    assert providers[0]["models"] == ["alpha-best", "alpha-extra"]
    assert providers[1]["registry_key_name"] == "Luka"
    assert providers[2]["registry_key_name"] == "Brother"
    assert group["strategy"] == "fallback"
    assert group["members"] == [
        {"provider": "free-registry-beta-env", "model": "beta-best"},
        {"provider": "free-registry-alpha-env", "model": "alpha-best"},
        {"provider": "free-registry-alpha-db-11", "model": "alpha-best"},
        {"provider": "free-registry-alpha-db-12", "model": "alpha-best"},
    ]


def test_build_overlay_uses_ui_key_when_environment_key_is_missing():
    providers, group = registry.build_registry_overlay(
        sample_registry(),
        environ={},
        stored_credentials=[
            {
                "id": 7,
                "provider_id": "alpha",
                "name": "Backup",
                "api_key": "alpha-backup",
                "enabled": True,
            }
        ],
    )

    assert [provider["name"] for provider in providers] == [
        "free-registry-alpha-db-7"
    ]
    assert group["members"] == [
        {"provider": "free-registry-alpha-db-7", "model": "alpha-best"}
    ]


def test_build_overlay_skips_disabled_and_duplicate_stored_keys():
    providers, group = registry.build_registry_overlay(
        sample_registry(),
        environ={"ALPHA_KEY": "same-secret"},
        stored_credentials=[
            {
                "id": 1,
                "provider_id": "alpha",
                "name": "Duplicate",
                "api_key": "same-secret",
                "enabled": True,
            },
            {
                "id": 2,
                "provider_id": "alpha",
                "name": "Disabled",
                "api_key": "disabled-secret",
                "enabled": False,
            },
        ],
    )

    assert [provider["name"] for provider in providers] == [
        "free-registry-alpha-env"
    ]
    assert group["members"] == [
        {"provider": "free-registry-alpha-env", "model": "alpha-best"}
    ]


def test_apply_registry_replaces_only_managed_providers_and_reserves_free_group():
    config = {
        "providers": [
            {"name": "user-provider", "models": ["user-model"]},
            {
                "name": "free-registry-old",
                "models": ["old"],
                "managed_by": registry.MANAGED_BY,
                "api_key": "old-secret",
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
        "free-registry-beta-env",
    ]
    assert "user-group" in config["groups"]
    assert config["groups"]["free-models"]["members"] == [
        {"provider": "free-registry-beta-env", "model": "beta-best"}
    ]


def test_strip_managed_overlay_removes_dynamic_secrets_before_yaml_persistence():
    config = {
        "providers": [
            {"name": "user", "api_key": "user-secret", "models": ["m"]},
            {
                "name": "free-registry-alpha-db-1",
                "api_key": "must-not-persist",
                "models": ["alpha-best"],
                "managed_by": registry.MANAGED_BY,
            },
        ],
        "groups": {
            "user": {"members": [{"provider": "user", "model": "m"}]},
            "free-models": {
                "members": [
                    {
                        "provider": "free-registry-alpha-db-1",
                        "model": "alpha-best",
                    }
                ],
                "managed_by": registry.MANAGED_BY,
            },
        },
    }

    clean = registry.strip_managed_overlay(config)

    assert clean["providers"] == [
        {"name": "user", "api_key": "user-secret", "models": ["m"]}
    ]
    assert set(clean["groups"]) == {"user"}
    assert "must-not-persist" not in str(clean)


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
