#!/usr/bin/env bash
# Real Docker staging fault injection: old container -> broken candidate ->
# automatically restored immutable old image, untouched SQLite.
set -Eeuo pipefail
command -v docker >/dev/null
command -v python3 >/dev/null
real_docker="$(command -v docker)"
original_path="$PATH"
work="$(mktemp -d)"
cleanup() {
  PATH="$original_path" "$real_docker" compose -f "$work/docker-compose.yml" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$work"
}
trap cleanup EXIT

cp server_deployment/update.sh server_deployment/backup_release.py server_deployment/check_release.py "$work/"
printf 'ADMIN_USERNAME=test\nADMIN_PASSWORD=local-test-password\n' > "$work/.env"
printf 'providers: []\ngroups: {}\n' > "$work/config.yml"
python3 - "$work/app.db" <<'PY'
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("CREATE TABLE important (id INTEGER PRIMARY KEY, note TEXT)")
    db.execute("INSERT INTO important VALUES (1,'retain')")
PY

cat > "$work/Dockerfile" <<'DOCKERFILE'
FROM python:3.11-slim
ARG RELEASE_MARKER
RUN printf '%s\n' "$RELEASE_MARKER" > /release-marker
WORKDIR /app
CMD ["python","-m","http.server","8000"]
DOCKERFILE
cat > "$work/docker-compose.yml" <<'COMPOSE'
name: simpleai-real-rollback-smoke
services:
  llm-proxy:
    image: simpleai-release-smoke:current
    container_name: llm-proxy
    restart: "no"
    volumes:
      - ./config.yml:/app/config.yml:ro
      - ./app.db:/app/app.db:rw
COMPOSE

"$real_docker" build -q --build-arg RELEASE_MARKER=old \
  -t simpleai-release-smoke:old "$work" >/dev/null
"$real_docker" build -q --build-arg RELEASE_MARKER=broken \
  -t simpleai-release-smoke:broken "$work" >/dev/null
"$real_docker" tag simpleai-release-smoke:old simpleai-release-smoke:current
"$real_docker" compose -f "$work/docker-compose.yml" up -d llm-proxy
old_id="$("$real_docker" inspect --format '{{.Image}}' llm-proxy)"
[[ "$old_id" == sha256:* ]]

# Keep candidate fully local; a separate Docker CLI shim intercepts only pull.
# Every image tag, container recreate, health check and rollback is REAL Docker.
mkdir -p "$work/fakebin"
cat > "$work/fakebin/docker" <<'SH'
#!/usr/bin/env bash
if [[ " $* " == *" pull llm-proxy "* ]]; then
  echo "Injected local candidate; remote pull bypassed in staging only." >&2
  exit 0
fi
exec "$REAL_DOCKER" "$@"
SH
chmod 700 "$work/fakebin/docker"
"$real_docker" tag simpleai-release-smoke:broken simpleai-release-smoke:current

set +e
(cd "$work" && REAL_DOCKER="$real_docker" PATH="$work/fakebin:$original_path" \
  bash ./update.sh > "$work/staging-release.log" 2>&1)
result="$?"
set -e
if [[ "$result" -eq 0 ]]; then
  cat "$work/staging-release.log"
  echo "FAIL: broken candidate was accepted." >&2
  exit 1
fi
if ! grep -q 'Previous image restored' "$work/staging-release.log"; then
  cat "$work/staging-release.log"
  echo "FAIL: attempted rollback was not confirmed." >&2
  exit 1
fi

restored_id="$("$real_docker" inspect --format '{{.Image}}' llm-proxy)"
[[ "$old_id" == "$restored_id" ]] || {
  echo "FAIL: container image ID differs from immutable previous image" >&2
  exit 1
}
"$real_docker" exec llm-proxy cat /release-marker | grep -qx old
python3 - "$work" <<'PY'
import pathlib, sqlite3, sys
root = pathlib.Path(sys.argv[1])
backups = list((root / ".release-backups").glob("*/app.db"))
assert len(backups) == 1, backups
for dbfile in [root / "app.db", backups[0]]:
    with sqlite3.connect(dbfile) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT note FROM important WHERE id=1").fetchone() == ("retain",)
PY
echo "PASS: real Docker rollback restored previous immutable image; SQLite preserved."
