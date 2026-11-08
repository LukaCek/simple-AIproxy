#!/usr/bin/env bash
# SimpleAI Managed Worker Updater v1
# Runs as the same unprivileged Linux user that installed the Compose worker.
# Docker socket is accessed ONLY by the host CLI, never mounted into a worker.
set -Eeuo pipefail
set +x
umask 077

die() { printf 'SimpleAI updater: %s\n' "$*" >&2; exit 1; }
log() { printf 'SimpleAI updater: %s\n' "$*"; }

root="$HOME/.local/share/simpleai-compute"
image="ghcr.io/lukacek/simple-aiproxy-worker:latest"
mode="${1:-}"
name="${2:-}"
[[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || die "Expected a valid worker name."
dir="$root/$name"
[[ -d "$dir" && ! -L "$dir" && -f "$dir/docker-compose.yml" ]] ||
  die "No installed worker at $dir"

check_service_image() {
  cd "$dir"
  docker compose config --format json |
    python3 -c 'import json,sys; cfg=json.load(sys.stdin); actual=cfg["services"]["compute-worker"]["image"]; assert actual=="ghcr.io/lukacek/simple-aiproxy-worker:latest", "Only production latest image can be auto-updated"' ||
      die "Refusing update: this is not a standard production Compute Worker."
}


# Optional one-time transition for pre-fix Auto Maximum installations.
# A new Docker cap changes model test eligibility but does not allocate RAM.
# This is explicit and never done during a scheduled image update.
migrate_auto_max() {
  [[ -f "$dir/.env" ]] || die "Missing existing worker .env."
  cd "$dir"
  local cid status started
  cid="$(docker compose ps -q compute-worker 2>/dev/null || :)"
  if [[ -n "$cid" ]]; then
    status="$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null || :)"
    if [[ "$status" == "running" ]] && ! worker_idle; then
      die "Worker is active; wait until it is idle before migrating its RAM limit."
    fi
  fi
  cp -p -- .env .env.before-auto-max
  python3 - ".env" <<'PY'
from pathlib import Path
import math, os, sys

p = Path(sys.argv[1])
lines = p.read_text(encoding='utf-8').splitlines(keepends=True)
required = {"WORKER_MEMORY_LIMIT", "WORKER_CPU_LIMIT", "WORKER_THREADS"}
found = {line.split("=", 1)[0] for line in lines if "=" in line}
if not required <= found:
    raise SystemExit("Not an Auto installer .env; refusing to change Manual settings.")
gib = 1024 ** 3
with open("/proc/meminfo", encoding="ascii") as stream:
    total = next(int(line.split()[1]) * 1024 for line in stream
                 if line.startswith("MemTotal:"))
reserve = max(2 * gib, total * 0.15)
limit = math.floor(max(0, total - reserve) / gib * 4) / 4
if limit < 2:
    raise SystemExit("Not enough total host RAM to safely run Auto Maximum.")
cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
new = {"WORKER_MEMORY_LIMIT": f"{limit}g",
       "WORKER_CPU_LIMIT": str(float(cpus)),
       "WORKER_THREADS": str(cpus)}
out = []
for line in lines:
    key = line.split("=", 1)[0]
    out.append(f"{key}={new[key]}\n" if key in new else line)
tmp = p.with_name(".env.auto-max.tmp")
fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.writelines(out)
os.replace(tmp, p)
print(f"Auto Maximum: {limit} GiB Docker ceiling on {total/gib:.2f} GiB host RAM; "
      f"{cpus} CPU cores")
PY
  if [[ $? -ne 0 ]]; then
    cp -p -- .env.before-auto-max .env
    rm -f -- .env.before-auto-max
    die "Unable to calculate Auto Maximum."
  fi
  started="$(date +%s)"
  if docker compose config --quiet &&
     docker compose up -d --no-build --no-deps --force-recreate --pull never compute-worker &&
     wait_connected "$started"; then
    rm -f -- .env.before-auto-max
    log "New resource limit active; original worker identity and model volumes retained."
    return 0
  fi
  log "RAM migration failed; restoring previous Compose settings."
  cp -p -- .env.before-auto-max .env
  rm -f -- .env.before-auto-max
  started="$(date +%s)"
  docker compose up -d --no-build --no-deps --force-recreate --pull never compute-worker ||
    die "Could not restore original worker container; check Compose logs."
  wait_connected "$started" ||
    die "Original RAM limit restored, but worker not connected. Check Docker logs."
  die "Auto Maximum migration rolled back; original RAM limit is restored."
}

install_timer() {
  [[ $# -eq 1 || $# -eq 2 ]] || die "Usage: updater --install WORKER_NAME https://YOUR_PROXY"
  local origin="$1"
  [[ "$origin" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] ||
    die "Installer endpoint must be a trusted public HTTPS origin."
  command -v systemctl >/dev/null && command -v loginctl >/dev/null ||
    die "Automatic updates require systemd and loginctl."
  command -v docker >/dev/null && command -v flock >/dev/null &&
    command -v python3 >/dev/null && command -v curl >/dev/null ||
    die "Automatic updates require Docker, flock, Python 3 and curl."
  docker info >/dev/null 2>&1 || die "Cannot contact the host Docker daemon."
  check_service_image
  if [[ "${2:-}" == "--recalculate-auto-max" ]]; then
    migrate_auto_max
  fi

  local dest="$dir/auto-update.sh" tmp
  if [[ -e "$dest" && ! -f "$dest" ]] ||
      { [[ -f "$dest" ]] && ! grep -Fq '# SimpleAI Managed Worker Updater v1' "$dest"; }; then
    die "Existing unmanaged updater will not be overwritten: $dest"
  fi
  tmp="$(mktemp "$dir/.auto-update.XXXXXXXX")"
  trap 'rm -f -- "$tmp"' EXIT
  curl -fsSL --connect-timeout 10 --max-time 45 \
    "$origin/compute/auto-update.sh" -o "$tmp" ||
    die "Unable to fetch the trusted HTTPS proxy's auto-update helper."
  head -n 5 "$tmp" | grep -Fq '# SimpleAI Managed Worker Updater v1' ||
    die "Proxy returned an unexpected updater file."
  bash -n "$tmp" || die "Downloaded updater failed bash syntax validation."
  chmod 700 "$tmp"
  mv -f -- "$tmp" "$dest"
  trap - EXIT

  local units="$HOME/.config/systemd/user"
  mkdir -p -- "$units"
  if [[ -f "$units/simpleai-compute-update@.service" ]] &&
     ! grep -Fq '# SimpleAI Managed Worker Updater v1' "$units/simpleai-compute-update@.service"; then
    die "Existing unmanaged service unit will not be overwritten."
  fi
  if [[ -f "$units/simpleai-compute-update@.timer" ]] &&
     ! grep -Fq '# SimpleAI Managed Worker Updater v1' "$units/simpleai-compute-update@.timer"; then
    die "Existing unmanaged timer unit will not be overwritten."
  fi
  cat > "$units/simpleai-compute-update@.service" <<'UNIT'
# SimpleAI Managed Worker Updater v1
[Unit]
Description=SimpleAI Compute Worker %i: update from GHCR if safe
[Service]
Type=oneshot
ExecStart=%h/.local/share/simpleai-compute/%i/auto-update.sh --check %i
TimeoutStartSec=30min
Nice=10
IOSchedulingClass=idle
UNIT
  cat > "$units/simpleai-compute-update@.timer" <<'UNIT'
# SimpleAI Managed Worker Updater v1
[Unit]
Description=SimpleAI Compute Worker %i: scheduled auto-update
[Timer]
OnCalendar=*-*-* 03,09,15,21:00:00
RandomizedDelaySec=30min
Persistent=true
[Install]
WantedBy=timers.target
UNIT

  # --user timers must continue running after the SSH session closes.
  # One-time sudo only enables lingering for THIS Linux account; the updater
  # itself never runs as root.
  local user linger
  user="$(id -un)"
  linger="$(loginctl show-user "$user" -p Linger --value 2>/dev/null || :)"
  if [[ "$linger" != "yes" ]]; then
    [[ -r /dev/tty && -w /dev/tty ]] ||
      die "One-time approval required: run 'sudo loginctl enable-linger $user' then retry setup."
    command -v sudo >/dev/null ||
      die "sudo required to enable user timers after logout."
    log "One-time sudo approval may be requested to keep updates active after logout."
    sudo loginctl enable-linger "$user" </dev/tty ||
      die "Could not enable lingering; background timer has NOT been enabled."
  fi

  systemctl --user daemon-reload ||
    die "Unable to connect to systemd user manager."
  systemctl --user enable --now "simpleai-compute-update@$name.timer" ||
    die "Timer could not be enabled. Check 'systemctl --user status simpleai-compute-update@$name.timer'."
  systemctl --user is-active --quiet "simpleai-compute-update@$name.timer" ||
    die "Timer is not active."
  log "Automatic updates enabled for $name (four checks daily; no SSH needed)."
  log "Status: systemctl --user status simpleai-compute-update@$name.timer"
}

worker_idle() {
  docker compose exec -T compute-worker python -c '
import json
from pathlib import Path
s=json.loads(Path("/data/worker-runtime-status.json").read_text())
assert s.get("phase") in ("idle", "disconnected", "connecting")
assert not s.get("models", {}).get("busy", True)
' >/dev/null 2>&1
}

worker_connected_since() {
  docker compose exec -T compute-worker python -c '
import json, pathlib, sys, time
p=pathlib.Path("/data/worker-runtime-status.json")
s=json.loads(p.read_text())
assert pathlib.Path("/data/identity.json").is_file()
assert s.get("connected") is True
assert s.get("updated_at", 0) >= int(sys.argv[1])
assert time.time() - s["updated_at"] <= 30
' "$1" >/dev/null 2>&1
}

wait_connected() {
  local started="$1"
  for _ in $(seq 1 36); do
    if worker_connected_since "$started"; then return 0; fi
    sleep 5
  done
  return 1
}

check_update() {
  [[ $# -eq 0 ]] || die "Usage: updater --check WORKER_NAME"
  command -v flock >/dev/null || die "flock missing."
  check_service_image
  cd "$dir"
  exec 9>"$dir/.update.lock"
  flock -n 9 || { log "Another check is running; skipped."; return 0; }
  local cid old new started rollback_tag status
  cid="$(docker compose ps -q compute-worker 2>/dev/null || :)"
  old=''
  if [[ -n "$cid" ]]; then
    old="$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null || :)"
    status="$(docker inspect --format '{{.State.Status}}' "$cid" 2>/dev/null || :)"
    if [[ "$status" == "running" ]] && ! worker_idle; then
      log "Worker is active or has unknown status; defer update."
      return 0
    fi
  fi
  [[ -n "$old" ]] || old="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || :)"

  # GHCR is the trusted release channel. No shell commands or scripts from
  # model responses or proxy-side WSS messages can trigger Docker updates.
  docker pull --quiet "$image" >/dev/null || die "Registry unavailable; keeping current worker."
  new="$(docker image inspect --format '{{.Id}}' "$image")"
  [[ -n "$new" ]] || die "Pulled release has no image ID."
  if [[ "$new" == "$old" ]]; then
    log "Already current: $new"
    return 0
  fi
  if [[ -s "$dir/.update-rejected-image" ]] &&
     [[ "$new" == "$(cat "$dir/.update-rejected-image")" ]]; then
    [[ -z "$old" ]] || docker image tag "$old" "$image"
    log "Previously failed image still published; waiting for a newer release."
    return 0
  fi

  # Avoid interrupting model inference that started while the image pulled.
  if [[ -n "$cid" && "$status" == "running" ]] && ! worker_idle; then
    [[ -z "$old" ]] || docker image tag "$old" "$image"
    log "Worker became busy during pull; defer upgrade."
    return 0
  fi
  rollback_tag="simpleai-compute-rollback:${name,,}"
  if [[ -n "$old" ]]; then docker image tag "$old" "$rollback_tag"; fi
  log "New release detected: $new. Recreating only worker $name."
  started="$(date +%s)"
  if docker compose up -d --no-build --no-deps --force-recreate --pull never compute-worker &&
     wait_connected "$started"; then
    rm -f -- "$dir/.update-rejected-image"
    log "Updated successfully; worker reconnected with the same identity."
    return 0
  fi
  printf '%s\n' "$new" > "$dir/.update-rejected-image"
  chmod 600 "$dir/.update-rejected-image"
  if [[ -n "$old" ]]; then
    log "New release failed health check; rolling back."
    docker image tag "$old" "$image"
    started="$(date +%s)"
    docker compose up -d --no-build --no-deps --force-recreate --pull never compute-worker ||
      die "Rollback container recreation failed. Check Docker logs."
    wait_connected "$started" ||
      die "Old worker restored but not connected. Inspect logs; new image blocked."
    log "Rollback successful; failed release will not be retried until its digest changes."
  else
    die "First release failed; no previous image available for rollback."
  fi
}

case "$mode" in
  --install)
    [[ $# -eq 3 || $# -eq 4 ]] ||
      die "Usage: updater --install WORKER_NAME https://YOUR_PROXY [--recalculate-auto-max]"
    [[ $# -ne 4 || "$4" == "--recalculate-auto-max" ]] ||
      die "Unknown install option."
    install_timer "$3" "${4:-}"
    ;;
  --check)
    [[ $# -eq 2 ]] || die "Usage: updater --check WORKER_NAME"
    check_update
    ;;
  --status)
    [[ $# -eq 2 ]] || die "Usage: updater --status WORKER_NAME"
    systemctl --user status "simpleai-compute-update@$name.timer" --no-pager
    ;;
  *) die "Usage: updater --install WORKER_NAME https://YOUR_PROXY | --check WORKER_NAME | --status WORKER_NAME" ;;
esac
