"""Verify the newly deployed SimpleAI proxy from inside its own container.

Read admin/API credentials from the running container, NEVER echo them.
Checks protected admin routes, compute schema and the real authenticated
/v1/models facade; returning nonzero triggers immutable-image rollback.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = "http://127.0.0.1:8000"


def get(route: str, authorization: str | None = None, max_bytes: int = 1024 * 1024):
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    request = Request(ROOT + route, headers=headers)
    with urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError(f"{route} returned HTTP {response.status}")
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise RuntimeError(f"{route} returned oversized body")
    return body


def verify():
    health = json.loads(get("/"))
    if health.get("status") != "ok":
        raise RuntimeError("Health JSON did not report status=ok")
    user = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin")
    authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    page = get("/admin/config", authorization)
    if b"Free provider API keys" not in page:
        raise RuntimeError("Admin configuration view is incomplete")
    page = get("/admin/compute-nodes", authorization)
    if b"Compute nodes" not in page:
        raise RuntimeError("Compute worker admin page is missing")
    nodes = json.loads(get("/admin/api/compute/nodes", authorization))
    if not isinstance(nodes.get("nodes"), list):
        raise RuntimeError("Compute worker registry schema unavailable")
    policy = json.loads(get("/admin/api/compute/model-policy", authorization))
    if not isinstance(policy.get("models"), list):
        raise RuntimeError("Compute model policy schema unavailable")

    # Inspect an existing key only in container memory; never log it.
    # The route requires bearer auth. If no key exists, require a real 401
    # rather than creating a production credential during a health check.
    with sqlite3.connect("/app/app.db", timeout=10) as db:
        row = db.execute("SELECT key FROM API_Keys ORDER BY id LIMIT 1").fetchone()
    if row:
        models = json.loads(get("/v1/models", "Bearer " + row[0]))
        if models.get("object") != "list" or not isinstance(models.get("data"), list):
            raise RuntimeError("Authenticated /v1/models returned invalid schema")
    else:
        try:
            get("/v1/models", "Bearer invalid-deploy-healthcheck-key")
        except HTTPError as exc:
            if exc.code != 401:
                raise RuntimeError(f"/v1/models route returned HTTP {exc.code}") from exc
        else:
            raise RuntimeError("/v1/models accepted a nonexistent key")
        print("WARN: no API key exists; /v1/models authentication gate verified")

    print("PASS: health, admin UI, compute registry/policy and /v1/models")


if __name__ == "__main__":
    verify()
