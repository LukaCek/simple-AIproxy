#!/bin/bash
set -e

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Starting deployment update..."

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Pulling latest Docker image..."
docker compose pull

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Restarting container..."
docker compose up -d

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Cleaning up dangling images..."
docker image prune -f

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Deployment update completed successfully!"
