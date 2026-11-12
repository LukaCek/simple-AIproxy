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

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Verifying live proxy image..."
EXPECTED_COMMIT_SHA="$(printenv EXPECTED_COMMIT_SHA || true)"
if [ -n "$EXPECTED_COMMIT_SHA" ]; then
  docker pull "ghcr.io/lukacek/simple-aiproxy:$EXPECTED_COMMIT_SHA" >/dev/null
  expected_image="$(docker image inspect "ghcr.io/lukacek/simple-aiproxy:$EXPECTED_COMMIT_SHA" --format '{{.Id}}')"
  actual_image="$(docker inspect llm-proxy --format '{{.Image}}')"
  if [ "$expected_image" != "$actual_image" ]; then
    echo "ERROR: Live proxy image differs from the expected Git commit." >&2
    exit 1
  fi
  echo "Verified running llm-proxy image matches expected commit."
fi

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Checking actual HTTPS NPM OAuth routing..."
curl --fail --silent --show-error --max-time 12 \
  --resolve aiproxy.cekluka.com:443:127.0.0.1 \
  "https://aiproxy.cekluka.com/.well-known/oauth-authorization-server" \
  | python3 -c 'import json,sys
data=json.load(sys.stdin)
if data.get("issuer") != "https://aiproxy.cekluka.com" or data.get("simpleai_consent_revision") != "idempotent-v2":
    sys.exit("ERROR: NPM routes OAuth to an outdated or incorrect server")
print("Verified NPM OAuth route revision: idempotent-v2")'

echo "[$(date +'%Y-%m-%d %H:%M:%S')] OAuth diagnostics (privacy-safe aggregate code counts)..."
docker compose exec -T llm-proxy python - <<'PY'
from collections import Counter
from datetime import datetime, timedelta, timezone
import simpleai_mcp_logging as log

allowed = {
    "authorization_code_exchange_succeeded", "authorization_code_reused",
    "authorization_code_not_found", "authorization_code_pkce_mismatch",
    "authorization_code_expired", "authorization_code_client_mismatch",
    "authorization_code_redirect_mismatch", "authorization_code_parameters_invalid",
    "token_resource_mismatch", "code_exchange_resource_omitted",
    "consent_request_replayed", "consent_replay_redirected",
    "consent_replay_after_completion", "consent_request_unknown",
    "consent_request_expired", "consent_csrf_invalid",
    "consent_origin_rejected", "refresh_token_reused_or_revoked",
    "consent_post_received", "consent_approved_redirected",
    "consent_denied_redirected",
}
counts = Counter()
cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
for event in log.events(limit=200)["events"]:
    if event.get("component") != "mcp_oauth" or event.get("code") not in allowed:
        continue
    try:
        at = datetime.fromisoformat(event["at"])
    except (ValueError, TypeError, KeyError):
        continue
    if at.tzinfo and at >= cutoff:
        counts[event["code"]] += 1
print("OAuth event codes only, 24-hour bounded history:")
for code, count in sorted(counts.items()):
    print("  %s: %d" % (code, count))
if not counts:
    print("  No recent OAuth events within the bounded history.")
PY

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Cleaning up dangling images..."
docker image prune -f

echo "[$(date +'%Y-%m-%d %H:%M:%S')] Deployment update completed successfully!"
