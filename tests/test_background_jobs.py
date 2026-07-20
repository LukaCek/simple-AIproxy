import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import background_jobs
import main


def setup_background_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.init_database()
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO API_Keys (name, key, created_at) VALUES (?, ?, ?)", ("test", "test-key", "now"))
        conn.commit()
    config = main.normalize_config_schema(
        {
            "providers": [],
            "groups": {},
            "model_profiles": {
                "slowbrain-70b": {
                    "type": "llamacpp_rpc",
                    "endpoint": "http://llama-main:8080/v1",
                    "health_url": "http://llama-main:8080/health",
                    "model": "llama-3.3-70b-q2",
                    "timeout_seconds": 21600,
                    "max_parallel_jobs": 1,
                    "max_attempts": 2,
                    "retry_delay_seconds": 0,
                },
                "prep": {"type": "small_prep", "max_attempts": 1},
            },
        }
    )
    main.config_data = config
    return db_path, config


def auth_headers():
    return {"Authorization": "Bearer test-key"}


def test_job_creation_status_logs_result_and_cancel(tmp_path, monkeypatch):
    _, config = setup_background_db(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "config_data", config)
        response = client.post(
            "/jobs",
            headers=auth_headers(),
            json={"model": "slowbrain-70b", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.1},
        )
        assert response.status_code == 200
        job_id = response.json()["id"]
        assert response.json()["status"] == "queued"

        status_response = client.get(f"/jobs/{job_id}", headers=auth_headers())
        assert status_response.status_code == 200
        assert status_response.json()["model_profile"] == "slowbrain-70b"

        pending_result = client.get(f"/jobs/{job_id}/result", headers=auth_headers())
        assert pending_result.status_code == 409

        logs_response = client.get(f"/jobs/{job_id}/logs", headers=auth_headers())
        assert logs_response.status_code == 200
        assert logs_response.json()["partial_output"] == ""

        cancel_response = client.post(f"/jobs/{job_id}/cancel", headers=auth_headers())
        assert cancel_response.status_code == 200
        assert cancel_response.json()["status"] == "cancelled"


def test_async_chat_background_extension_enqueues_without_sync_proxy_call(tmp_path, monkeypatch):
    _, config = setup_background_db(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "config_data", config)
        response = client.post(
            "/v1/chat/completions",
            headers=auth_headers(),
            json={"model": "slowbrain-70b", "background": True, "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "background.chat.completion"
    assert body["status"] == "queued"
    stored = background_jobs.get_job(tmp_path / "app.db", body["id"])
    assert stored["request_payload"]["model"] == "slowbrain-70b"
    assert "background" not in stored["request_payload"]


def test_sqlite_persistence_and_lease_retry_basics(tmp_path, monkeypatch):
    db_path, _ = setup_background_db(tmp_path, monkeypatch)
    job = background_jobs.create_job(
        db_path,
        model_profile="slowbrain-70b",
        worker_type="llamacpp_rpc_worker",
        request_payload={"model": "slowbrain-70b", "messages": []},
        max_attempts=2,
    )
    leased = background_jobs.lease_next_job(db_path, "worker-a", ["llamacpp_rpc_worker"], lease_seconds=1)
    assert leased["id"] == job["id"]
    assert leased["status"] == "leased"
    assert leased["attempt_count"] == 1

    retried = background_jobs.fail_or_retry_job(db_path, job["id"], "worker-a", "temporary", retry_delay_seconds=0)
    assert retried["status"] == "retry_wait"
    leased_again = background_jobs.lease_next_job(db_path, "worker-b", ["llamacpp_rpc_worker"], lease_seconds=1)
    assert leased_again["attempt_count"] == 2
    failed = background_jobs.fail_or_retry_job(db_path, job["id"], "worker-b", "boom", retry_delay_seconds=0)
    assert failed["status"] == "failed"


def test_config_validation_and_monitoring_endpoints(tmp_path, monkeypatch):
    _, config = setup_background_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "config_data", config)
    assert main.get_model_profile("slowbrain-70b")["worker_type"] == "llamacpp_rpc_worker"
    assert main.get_model_profile("prep")["worker_type"] == "small_prep_worker"
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "config_data", config)
        workers = client.get("/workers", headers=auth_headers())
        assert workers.status_code == 200
        assert workers.json()["data"][0]["max_parallel_jobs"] == 1
        stats = client.get("/jobs/stats", headers=auth_headers())
        assert stats.status_code == 200
        metrics = client.get("/metrics", headers=auth_headers())
        assert metrics.status_code == 200
        assert "aiproxy_background_jobs" in metrics.text
        models = client.get("/v1/models", headers=auth_headers())
        assert any(item["id"] == "slowbrain-70b" for item in models.json()["data"])
