import importlib
import sqlite3
import sys
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main_impl as impl

# Register the route on the shared FastAPI application.
import audio_transcriptions


class FakeAudioClient:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {"text": "Ciao Luka"}
        self.requests = []

    async def post(self, url, data=None, files=None, headers=None, timeout=None, **kwargs):
        self.requests.append(
            {
                "url": url,
                "data": data,
                "files": files,
                "headers": headers,
                "timeout": timeout,
            }
        )
        request = httpx.Request("POST", url)
        return httpx.Response(self.status_code, json=self.payload, request=request)


class FallbackAudioClient(FakeAudioClient):
    async def post(self, url, data=None, files=None, headers=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "data": data, "files": files, "headers": headers, "timeout": timeout})
        request = httpx.Request("POST", url)
        if "first.local" in url:
            return httpx.Response(429, json={"error": "rate limit"}, request=request)
        return httpx.Response(200, json={"text": "fallback ok"}, request=request)


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


def transcription_config(url="https://api.groq.com/openai/v1"):
    return {
        "providers": [
            {
                "name": "groq-stt",
                "url": url,
                "api_key": "groq-secret",
                "api_mode": "openai_audio_transcriptions",
                "models": ["whisper-large-v3-turbo"],
            }
        ],
        "groups": {},
    }


def test_resolve_audio_transcription_url():
    assert audio_transcriptions.resolve_audio_transcription_url({"url": "https://api.groq.com/openai/v1"}) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert audio_transcriptions.resolve_audio_transcription_url({"url": "https://example.test"}) == "https://example.test/v1/audio/transcriptions"
    assert audio_transcriptions.resolve_audio_transcription_url({"url": "https://example.test/v1/audio/transcriptions"}) == "https://example.test/v1/audio/transcriptions"


def test_transcription_requires_proxy_api_key(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    impl.config_data = transcription_config()
    with TestClient(impl.app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("sample.webm", b"audio", "audio/webm")},
            data={"model": "whisper-large-v3-turbo", "language": "it"},
        )
    assert response.status_code == 401


def test_transcription_forwards_openai_compatible_multipart(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeAudioClient()
    monkeypatch.setattr(impl, "http_client", fake)
    impl.config_data = transcription_config()

    with TestClient(impl.app) as client:
        monkeypatch.setattr(impl, "http_client", fake)
        impl.config_data = transcription_config()
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"fake-audio", "audio/webm")},
            data={
                "model": "whisper-large-v3-turbo",
                "language": "it",
                "prompt": "Italian conversation",
                "response_format": "json",
                "temperature": "0",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"text": "Ciao Luka"}
    sent = fake.requests[0]
    assert sent["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert sent["data"]["model"] == "whisper-large-v3-turbo"
    assert sent["data"]["language"] == "it"
    assert sent["data"]["prompt"] == "Italian conversation"
    assert sent["files"]["file"][0] == "sample.webm"
    assert sent["files"]["file"][1] == b"fake-audio"
    assert sent["files"]["file"][2] == "audio/webm"
    assert sent["headers"]["Authorization"] == "Bearer groq-secret"


def test_transcription_provider_key_can_come_from_env(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FakeAudioClient()
    monkeypatch.setattr(impl, "http_client", fake)
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    config = transcription_config()
    config["providers"][0]["api_key"] = ""
    config["providers"][0]["api_key_env"] = "GROQ_API_KEY"
    impl.config_data = config

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
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer from-env"


def test_transcription_falls_back_on_rate_limit(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    fake = FallbackAudioClient()
    monkeypatch.setattr(impl, "http_client", fake)
    impl.config_data = {
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

    with TestClient(impl.app) as client:
        monkeypatch.setattr(impl, "http_client", fake)
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"audio", "audio/webm")},
            data={"model": "whisper-large-v3-turbo"},
        )

    assert response.status_code == 200
    assert response.json()["text"] == "fallback ok"
    assert [httpx.URL(req["url"]).host for req in fake.requests] == ["first.local", "second.local"]


def test_transcription_rejects_unknown_model(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    impl.config_data = transcription_config()
    with TestClient(impl.app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"audio", "audio/webm")},
            data={"model": "not-configured"},
        )
    assert response.status_code == 404


def test_transcription_rejects_oversized_audio(tmp_path, monkeypatch):
    setup_key_db(tmp_path, monkeypatch)
    impl.config_data = transcription_config()
    monkeypatch.setattr(audio_transcriptions, "MAX_AUDIO_BYTES", 4)
    with TestClient(impl.app) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            headers={"Authorization": "Bearer proxy-key"},
            files={"file": ("sample.webm", b"12345", "audio/webm")},
            data={"model": "whisper-large-v3-turbo"},
        )
    assert response.status_code == 413
