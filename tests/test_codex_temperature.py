import runtime_entrypoint


def test_codex_payload_drops_temperature_from_home_assistant_request():
    payload = {
        "model": "homeassistant-cloud",
        "messages": [
            {"role": "system", "content": "Manage Home Assistant."},
            {"role": "user", "content": "Is the aquarium light on?"},
        ],
        "temperature": 0.3,
        "stream": False,
    }

    converted = runtime_entrypoint.chat_to_responses_payload(
        payload,
        "gpt-5.6-sol",
    )

    assert converted["model"] == "gpt-5.6-sol"
    assert converted["stream"] is True
    assert "temperature" not in converted
    assert converted["input"] == [
        {"role": "user", "content": "Is the aquarium light on?"}
    ]
    assert converted["instructions"] == "Manage Home Assistant."
