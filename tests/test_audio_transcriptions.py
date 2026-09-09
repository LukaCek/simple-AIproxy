import sqlite3
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main_impl as impl
import audio_transcriptions


class FakeAudioClient:
    def __init__(self, fallback=False):
        self.fallback = fallback
        self.requests = []

    async def post(self, url, data=None, files=None, headers=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "data": data, "files": files, "headers": headers, "timeout": timeout})
        request = httpx.Request("POST", url)
        if self.fallback and "first.local" in url:
            return httpx.Response(429, json={"error": "rate limit"}, request=request)
        text = "fallback ok" if self.fallback else "Ciao Luka"
        return httpx.Response(200, json={"text": text}, request=request)


def setup_key_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(impl, "DB_PATH", db_path)
    impl.init_database()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)",
            ("pinme", "proxy-key", "now"),
        )
        conn.commit()


def one_provider_config():
    return {
        "providers": [
            {
                "name": "speech",
                "url": "https://speech.local/v1",
                "api_key": "provider-key",
                "api_mode": "openai_audio_transcriptions",
                "models": ["whisper-large-v3-turbo"],
            }
        ],
        "groups": {},
    }


def two_provider_config():
    return {
        "providers": [
            {
                "name": "first",
                "url": "https://first.local/v1",
                "api_key": "one",
                "api_mode": "openai_audio_transcriptions",
                "models": ["whisper-large-v3-turbo"],
            },
            {
                "name": "second",
                "url": "https://second.local/v1",
                "api_key": "two",
                "api_mode": "openai_audio_transcriptions",
                "models": ["whisper-large-v3-turbo"],
            },
        ],
        "groups": {},
    }


def test_resolve_audio_transcription_url():
    assert audio_transcriptions.resolve_audio_transcription_url({"url": "https://example.test/v1"}) == "https://example.test/v1/audio/transcriptions"
    assert audio_transcriptions.resolve_audio_transcription_url({"url": "https://example.test"}) == "https://example.test/v1/audio/transcriptions"


def test_transcription_requires_proxy_auth(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    with TestClient(impl.app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("sample.webm", b"audio", "audio/webm")},
            data={"model": "whisper-large-v3-turbo"},
        )
    assert response.status_code == 401


def test_transcription_forwards_multipart(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeAudioClient()
    config = one_provider_config()
    with TestClient(impl.app) as client:
        monkeypatch.setattr(impl, "http_client", fake)
        impl.config_data = config
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"fake-audio", "audio/webm")},
            data={"model": "whisper-large-v3-turbo", "language": "it", "response_format": "json"},
        )

    assert response.status_code == 200
    assert response.json() == {"text": "Ciao Luka"}
    sent = fake.requests[0]
    assert sent["url"] == "https://speech.local/v1/audio/transcriptions"
    assert sent["data"]["model"] == "whisper-large-v3-turbo"
    assert sent["data"]["language"] == "it"
    assert sent["files"]["file"] == ("sample.webm", b"fake-audio", "audio/webm")
    assert sent["headers"]["Authorization"] == "Bearer provider-key"


def test_transcription_uses_provider_key_from_env(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeAudioClient()
    config = one_provider_config()
    config["providers"][0]["api_key"] = ""
    config["providers"][0]["api_key_env"] = "TEST_STT_KEY"
    monkeypatch.setenv("TEST_STT_KEY", "env-provider-key")

    with TestClient(impl.app) as client:
        monkeypatch.setattr(impl, "http_client", fake)
        impl.config_data = config
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"audio", "audio/webm")},
            data={"model": "whisper-large-v3-turbo"},
        )

    assert response.status_code == 200
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer env-provider-key"


def test_transcription_falls_back_after_rate_limit(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeAudioClient(fallback=True)
    config = two_provider_config()

    with TestClient(impl.app) as client:
        monkeypatch.setattr(impl, "http_client", fake)
        impl.config_data = config
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"audio", "audio/webm")},
            data={"model": "whisper-large-v3-turbo"},
        )

    assert response.status_code == 200
    assert response.json()["text"] == "fallback ok"
    assert [httpx.URL(item["url"]).host for item in fake.requests] == ["first.local", "second.local"]


def test_unknown_transcription_model_is_404(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    with TestClient(impl.app) as client:
        impl.config_data = one_provider_config()
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"audio", "audio/webm")},
            data={"model": "not-configured"},
        )
    assert response.status_code == 404


def test_oversized_audio_is_413(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    monkeypatch.setattr(audio_transcriptions, "MAX_AUDIO_BYTES", 4)
    with TestClient(impl.app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"12345", "audio/webm")},
            data={"model": "whisper-large-v3-turbo"},
        )
    assert response.status_code == 413
