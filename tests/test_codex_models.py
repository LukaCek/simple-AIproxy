import importlib

import main


def load_extension(monkeypatch, default_model=None, models=None):
    if default_model is None:
        monkeypatch.delenv("CODEX_DEFAULT_MODEL", raising=False)
    else:
        monkeypatch.setenv("CODEX_DEFAULT_MODEL", default_model)
    if models is None:
        monkeypatch.delenv("CODEX_MODELS", raising=False)
    else:
        monkeypatch.setenv("CODEX_MODELS", models)

    import codex_entrypoint

    return importlib.reload(codex_entrypoint)


def test_default_codex_models_include_selectable_variants(monkeypatch):
    extension = load_extension(monkeypatch)

    provider = extension.get_default_codex_provider()

    assert provider["models"] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]


def test_codex_default_model_can_be_changed_with_environment(monkeypatch):
    extension = load_extension(monkeypatch, default_model="gpt-5.6-terra")

    provider = extension.get_default_codex_provider()

    assert provider["models"][0] == "gpt-5.6-terra"
    assert set(provider["models"]) == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    }


def test_existing_codex_provider_gets_models_and_groups(monkeypatch):
    extension = load_extension(monkeypatch)
    config = {
        "providers": [
            {
                "name": "codex-main",
                "url": "https://chatgpt.com/backend-api/codex",
                "api_mode": "codex_responses",
                "oauth": True,
                "models": ["gpt-5.5"],
            }
        ],
        "groups": {},
    }

    normalized = extension.normalize_config_schema(config)
    provider = normalized["providers"][0]

    assert provider["models"] == [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]
    assert normalized["groups"]["codex"]["members"] == [
        {"provider": "codex-main", "model": "gpt-5.6-sol"}
    ]
    for model in provider["models"]:
        assert {"provider": "codex-main", "model": model} in normalized["groups"][model]["members"]


def test_custom_codex_models_are_supported(monkeypatch):
    extension = load_extension(
        monkeypatch,
        default_model="custom-codex",
        models="custom-codex,gpt-5.5",
    )

    assert extension.configured_codex_models() == ["custom-codex", "gpt-5.5"]
