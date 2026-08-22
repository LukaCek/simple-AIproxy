#!/bin/bash
set -euo pipefail

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Starting deployment update..."

# Keep the registry cache persistent across container recreation.
mkdir -p cache

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Pulling latest Docker image..."
docker compose pull

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restarting container..."
docker compose up -d

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Waiting for FastAPI health check..."
for i in $(seq 1 30); do
  if docker compose exec -T llm-proxy python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=2).read()" >/dev/null 2>&1; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] FastAPI is healthy."
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] FastAPI health check failed."
    docker compose ps
    docker compose logs --tail=200 llm-proxy
    exit 1
  fi
  sleep 2
done

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Verifying Free provider API keys UI..."
docker compose exec -T llm-proxy python - <<'PY'
import base64
import os
import sys
import urllib.request

username = os.environ.get("ADMIN_USERNAME", "admin")
password = os.environ.get("ADMIN_PASSWORD", "admin")
token = base64.b64encode(f"{username}:{password}".encode()).decode()
request = urllib.request.Request(
    "http://127.0.0.1:8000/admin/config",
    headers={"Authorization": f"Basic {token}"},
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8", errors="replace")
except Exception as exc:
    print(f"Failed to open /admin/config: {exc}", file=sys.stderr)
    sys.exit(1)

marker = "Free provider API keys"
if marker not in body:
    print(f"/admin/config is reachable but missing expected UI marker: {marker}", file=sys.stderr)
    sys.exit(1)
print("Verified /admin/config contains Free provider API keys UI.")
PY

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Cleaning up dangling images..."
docker image prune -f

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Deployment update completed successfully!"
