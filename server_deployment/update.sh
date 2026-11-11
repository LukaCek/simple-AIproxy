#!/usr/bin/env bash
# Transaction-safe SimpleAI proxy replacement. A failed post-deploy check
# automatically restores the old immutable image but NEVER overwrites live DB.
set -Eeuo pipefail
umask 077
cd "$(dirname "$0")"

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

command -v python3 >/dev/null || die "python3 is required on the production host"
command -v flock >/dev/null || die "flock is required for deploy locking"
docker compose -f docker-compose.yml config --quiet
exec 9> .simpleai-release.lock
flock -n 9 || die "Another SimpleAI deployment is already active"

previous_image="$(docker inspect --format '{{.Image}}' llm-proxy)"
[[ "$previous_image" =~ ^sha256:[a-f0-9]{64}$ ]] ||
    die "No trusted previous production image. Refusing replacement."

# Take the snapshot before pulling/replacing containers.
snapshot="$(python3 backup_release.py "$PWD" "$previous_image")"
[[ -f "$snapshot/app.db" && -f "$snapshot/metadata.json" ]] ||
    die "Backup is not complete; refusing deployment"
log "Verified SQLite snapshot and private config backup: $snapshot"

# Tag the exact local image ID, never the mutable :latest tag. Keep this tag
# and the private snapshot until the next manual release review.
release_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
rollback_image="simpleai-proxy-rollback:$release_id"
docker image tag "$previous_image" "$rollback_image"

phase="preflight"
rollback() {
  local code="$1"
  trap - ERR
  set +e
  log "Deployment failed (code $code) in phase $phase."
  if [[ "$phase" != "replacing" ]]; then
    log "Existing container was not replaced; leaving it untouched."
    log "Snapshot retained: $snapshot"
    exit "$code"
  fi
  log "Restoring previous immutable proxy image: $rollback_image"
  local override
  override="$(mktemp "$PWD/.simpleai-rollback.XXXXXXXX.yml")"
  printf 'services:\n  llm-proxy:\n    image: %s\n    pull_policy: never\n' "$rollback_image" > "$override"
  if docker compose -f docker-compose.yml -f "$override" up -d \
      --no-deps --force-recreate llm-proxy; then
    # Docker reports "Started" before FastAPI has bound port 8000.
    # Wait for the old image to become healthy instead of reporting a
    # false rollback failure due to a transient connection-refused.
    local restored_ready=0
    for i in $(seq 1 45); do
      if docker compose -f docker-compose.yml -f "$override" exec -T llm-proxy \
          python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).read()" >/dev/null 2>&1; then
        restored_ready=1
        break
      fi
      sleep 2
    done
    if [[ "$restored_ready" == 1 ]]; then
      log "Previous image restored and HTTP health check passed."
    else
      log "URGENT: rollback container started but health check FAILED."
      log "Database may require manual schema-compatible restoration."
    fi
  else
    log "URGENT: rollback recreation FAILED; manual intervention required."
  fi
  rm -f "$override"
  log "DO NOT blindly overwrite live app.db. Verified snapshot: $snapshot"
  exit "$code"
}
trap 'rollback "$?"' ERR

log "Pulling candidate image (prior image and SQLite snapshot retained)."
docker compose -f docker-compose.yml pull llm-proxy

# The current service bind mounts, external proxy network, config and cache
# remain unchanged. No volume removal, database restore or image prune.
phase="replacing"
log "Recreating only the llm-proxy service."
docker compose -f docker-compose.yml up -d --no-deps --force-recreate llm-proxy

log "Waiting for container HTTP readiness."
ready=0
for i in $(seq 1 45); do
  if docker compose -f docker-compose.yml exec -T llm-proxy python -c \
     "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != 1 ]]; then
  log "New container failed HTTP readiness"
  false  # Trigger ERR trap and immutable-image rollback.
fi

log "Checking admin UI, compute policy and authenticated /v1/models."
docker compose -f docker-compose.yml exec -T llm-proxy python - < check_release.py

trap - ERR
phase="complete"
log "DEPLOY PASS. Current image: $(docker inspect --format '{{.Image}}' llm-proxy)"
log "Retained snapshot: $snapshot"
log "Retained rollback image: $rollback_image"
