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

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Cleaning up dangling images..."
docker image prune -f

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Deployment update completed successfully!"
