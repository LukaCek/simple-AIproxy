import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

JOB_STATUSES = {"queued", "leased", "running", "retry_wait", "succeeded", "failed", "cancelled"}
WORKER_TYPES = {"llamacpp_rpc_worker", "airllm_offload_worker", "accelerate_offload_worker", "small_prep_worker"}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def utcnow() -> str:
    return datetime.utcnow().isoformat()


def init_job_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS BackgroundJobs (
                id TEXT PRIMARY KEY,
                model_profile TEXT NOT NULL,
                worker_type TEXT NOT NULL,
                request_payload TEXT NOT NULL,
                messages TEXT NOT NULL,
                params TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 1,
                lease_expires_at TEXT,
                worker_id TEXT,
                partial_output TEXT,
                final_output TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_background_jobs_status ON BackgroundJobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_background_jobs_lease ON BackgroundJobs(status, lease_expires_at)")
        conn.commit()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def decode_job(row: sqlite3.Row) -> Dict[str, Any]:
    job = dict(row)
    for key in ("request_payload", "messages", "params"):
        try:
            job[key] = json.loads(job[key]) if job.get(key) else ([] if key == "messages" else {})
        except Exception:
            job[key] = job.get(key)
    return job


def create_job(
    db_path: Path,
    *,
    model_profile: str,
    worker_type: str,
    request_payload: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    max_attempts: int = 1,
) -> Dict[str, Any]:
    if not model_profile:
        raise ValueError("model_profile is required")
    if worker_type not in WORKER_TYPES:
        raise ValueError(f"Unsupported worker_type: {worker_type}")
    payload = dict(request_payload)
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    params = dict(params or {})
    job_id = "job_" + uuid.uuid4().hex
    now = utcnow()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO BackgroundJobs (
                id, model_profile, worker_type, request_payload, messages, params, status,
                attempt_count, max_attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
            """,
            (job_id, model_profile, worker_type, json.dumps(payload), json.dumps(messages), json.dumps(params), max(1, int(max_attempts)), now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM BackgroundJobs WHERE id = ?", (job_id,)).fetchone()
        return decode_job(row)


def get_job(db_path: Path, job_id: str) -> Optional[Dict[str, Any]]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM BackgroundJobs WHERE id = ?", (job_id,)).fetchone()
        return decode_job(row) if row else None


def cancel_job(db_path: Path, job_id: str) -> Optional[Dict[str, Any]]:
    now = utcnow()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT status FROM BackgroundJobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        if row["status"] not in TERMINAL_STATUSES:
            conn.execute("UPDATE BackgroundJobs SET status = 'cancelled', finished_at = ?, lease_expires_at = NULL WHERE id = ?", (now, job_id))
            conn.commit()
        row = conn.execute("SELECT * FROM BackgroundJobs WHERE id = ?", (job_id,)).fetchone()
        return decode_job(row)


def append_partial_output(db_path: Path, job_id: str, text: str) -> None:
    if not text:
        return
    with _connect(db_path) as conn:
        conn.execute("UPDATE BackgroundJobs SET partial_output = COALESCE(partial_output, '') || ? WHERE id = ?", (text, job_id))
        conn.commit()


def list_recent_jobs(db_path: Path, limit: int = 50) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM BackgroundJobs ORDER BY created_at DESC LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
        return [decode_job(row) for row in rows]


def job_stats(db_path: Path) -> Dict[str, Any]:
    init_job_schema(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM BackgroundJobs GROUP BY status").fetchall()
    counts = {status: 0 for status in JOB_STATUSES}
    counts.update({row["status"]: row["count"] for row in rows})
    return {"total": sum(counts.values()), "statuses": counts}


def lease_next_job(db_path: Path, worker_id: str, worker_types: Optional[List[str]] = None, lease_seconds: int = 300) -> Optional[Dict[str, Any]]:
    worker_types = [w for w in (worker_types or list(WORKER_TYPES)) if w in WORKER_TYPES]
    if not worker_types:
        return None
    now = utcnow()
    lease_until = (datetime.utcnow() + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    placeholders = ",".join("?" for _ in worker_types)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"""
            SELECT * FROM BackgroundJobs
            WHERE worker_type IN ({placeholders})
              AND (
                status = 'queued'
                OR (status = 'retry_wait' AND (lease_expires_at IS NULL OR lease_expires_at <= ?))
                OR (status IN ('leased', 'running') AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
              )
            ORDER BY created_at ASC
            LIMIT 1
            """,
            [*worker_types, now, now],
        ).fetchone()
        if not row:
            conn.commit()
            return None
        conn.execute(
            """
            UPDATE BackgroundJobs
            SET status = 'leased', worker_id = ?, lease_expires_at = ?, attempt_count = attempt_count + 1,
                started_at = COALESCE(started_at, ?)
            WHERE id = ?
            """,
            (worker_id, lease_until, now, row["id"]),
        )
        conn.commit()
        return get_job(db_path, row["id"])


def heartbeat_job(db_path: Path, job_id: str, worker_id: str, lease_seconds: int = 300) -> Optional[Dict[str, Any]]:
    lease_until = (datetime.utcnow() + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    with _connect(db_path) as conn:
        conn.execute("UPDATE BackgroundJobs SET status = 'running', lease_expires_at = ? WHERE id = ? AND worker_id = ? AND status IN ('leased', 'running')", (lease_until, job_id, worker_id))
        conn.commit()
    return get_job(db_path, job_id)


def complete_job(db_path: Path, job_id: str, worker_id: str, output: Any) -> Optional[Dict[str, Any]]:
    now = utcnow()
    final_output = output if isinstance(output, str) else json.dumps(output)
    with _connect(db_path) as conn:
        conn.execute("UPDATE BackgroundJobs SET status = 'succeeded', final_output = ?, finished_at = ?, lease_expires_at = NULL WHERE id = ? AND worker_id = ?", (final_output, now, job_id, worker_id))
        conn.commit()
    return get_job(db_path, job_id)


def fail_or_retry_job(db_path: Path, job_id: str, worker_id: str, error: str, retry_delay_seconds: int = 60) -> Optional[Dict[str, Any]]:
    now = utcnow()
    retry_at = (datetime.utcnow() + timedelta(seconds=max(0, int(retry_delay_seconds)))).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT attempt_count, max_attempts FROM BackgroundJobs WHERE id = ? AND worker_id = ?", (job_id, worker_id)).fetchone()
        if not row:
            return get_job(db_path, job_id)
        if int(row["attempt_count"]) < int(row["max_attempts"]):
            conn.execute("UPDATE BackgroundJobs SET status = 'retry_wait', last_error = ?, lease_expires_at = ? WHERE id = ? AND worker_id = ?", (error, retry_at, job_id, worker_id))
        else:
            conn.execute("UPDATE BackgroundJobs SET status = 'failed', last_error = ?, finished_at = ?, lease_expires_at = NULL WHERE id = ? AND worker_id = ?", (error, now, job_id, worker_id))
        conn.commit()
    return get_job(db_path, job_id)


def cleanup_expired_leases(db_path: Path) -> int:
    now = utcnow()
    with _connect(db_path) as conn:
        cursor = conn.execute("UPDATE BackgroundJobs SET status = 'queued', worker_id = NULL, lease_expires_at = NULL WHERE status IN ('leased', 'running') AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?", (now,))
        conn.commit()
        return int(cursor.rowcount or 0)
