#!/usr/bin/env bash
# SimpleAI Compute legacy adoption helper: opt into scheduled image updates
# on every installed production worker owned by this Linux user.
set -Eeuo pipefail
set +x
umask 077

origin="${1:-}"
if [[ ! "$origin" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
  echo "Usage: bash enable-auto-updates.sh https://YOUR_PROXY_DOMAIN" >&2
  exit 2
fi
command -v docker >/dev/null && command -v curl >/dev/null ||
  { echo "Docker and curl are required." >&2; exit 1; }
docker info >/dev/null 2>&1 ||
  { echo "Docker is not accessible for this user. Re-login after group permission changes." >&2; exit 1; }

root="$HOME/.local/share/simpleai-compute"
shopt -s nullglob
configs=( "$root"/*/docker-compose.yml )
if (( ${#configs[@]} == 0 )); then
  echo "No installed SimpleAI workers under $root" >&2
  exit 1
fi

tmp="$(mktemp)"
trap 'rm -f -- "$tmp"' EXIT
curl -fsSL --connect-timeout 10 --max-time 45 \
  "$origin/compute/auto-update.sh" -o "$tmp" ||
  { echo "Cannot download update helper. Check production deployment." >&2; exit 1; }
grep -Fq '# SimpleAI Managed Worker Updater v1' "$tmp" ||
  { echo "Unexpected updater response." >&2; exit 1; }
bash -n "$tmp" || exit 1

success=0
failed=0
for config in "${configs[@]}"; do
  dir="$(dirname "$config")"
  name="$(basename "$dir")"
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || continue
  if [[ ! -f "$dir/.env" ]]; then
    echo "Skipping $name: missing .env."
    continue
  fi
  worker_origin="$(sed -n 's/^SIMPLEAI_PROXY_URL=//p' "$dir/.env")"
  if [[ "$worker_origin" != "$origin" ]]; then
    echo "Skipping $name: worker uses a different proxy."
    continue
  fi
  if ! (cd "$dir" && docker compose config --format json | python3 -c '
import json, sys
cfg=json.load(sys.stdin)
assert cfg["services"]["compute-worker"]["image"]=="ghcr.io/lukacek/simple-aiproxy-worker:latest"
' >/dev/null 2>&1); then
    echo "Skipping $name: local test or nonstandard image (not auto-updated)."
    continue
  fi
  echo "Enabling automatic updates for $name..."
  if bash "$tmp" --install "$name" "$origin"; then
    success=$((success+1))
  else
    failed=$((failed+1))
    echo "Updater FAILED for $name; existing worker remains running." >&2
  fi
done

# Install the same managed, safe TUI launcher as the current one-line worker
# installer, including multi-worker selection. Never overwrite unmanaged files.
bin="$HOME/.local/bin"
launcher="$bin/simpleai-tui"
mkdir -p "$bin"
if [[ ! -e "$launcher" && ! -L "$launcher" ]] ||
   { [[ -f "$launcher" ]] && grep -Fq '# SimpleAI Compute Worker managed TUI shortcut' "$launcher"; }; then
  out="$(mktemp "$bin/.simpleai-tui.XXXXXXXX")"
  cat > "$out" <<'SIMPLEAI_TUI_LAUNCHER'
#!/usr/bin/env bash
# SimpleAI Compute Worker managed TUI shortcut
set -euo pipefail

root="$HOME/.local/share/simpleai-compute"
usage() {
  printf 'Usage: simpleai-tui [worker-name] [--once]\n' >&2
}
name=''
once=0
for arg in "$@"; do
  case "$arg" in
    --once) once=1 ;;
    --help|-h) usage; exit 0 ;;
    -*)
      usage
      exit 2
      ;;
    *)
      if [[ -n "$name" ]]; then usage; exit 2; fi
      name="$arg"
      ;;
  esac
done

if [[ -n "$name" ]]; then
  if [[ ! "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]; then
    printf 'Invalid worker name.\n' >&2
    exit 2
  fi
  dir="$root/$name"
  if [[ ! -f "$dir/docker-compose.yml" ]]; then
    printf 'Worker "%s" not installed at %s\n' "$name" "$dir" >&2
    exit 1
  fi
else
  shopt -s nullglob
  configs=( "$root"/*/docker-compose.yml )
  if (( ${#configs[@]} == 0 )); then
    printf 'No installed SimpleAI Compute Workers found.\n' >&2
    exit 1
  fi
  if (( ${#configs[@]} > 1 )); then
    printf 'More than one worker is installed. Choose one:\n' >&2
    for config in "${configs[@]}"; do
      printf '  simpleai-tui %s\n' "$(basename "$(dirname "$config")")" >&2
    done
    exit 2
  fi
  dir="$(dirname "${configs[0]}")"
fi

cd "$dir"
if (( once )); then
  exec docker compose exec -T compute-worker python tui.py --once
fi
if [[ ! -t 0 || ! -t 1 ]]; then
  printf 'Interactive dashboard requires a terminal; use --once for a snapshot.\n' >&2
  exit 2
fi
exec docker compose exec compute-worker python tui.py
SIMPLEAI_TUI_LAUNCHER
  chmod 700 "$out"
  mv -f -- "$out" "$launcher"
  if [[ ":$PATH:" != *":$bin:"* ]]; then
    case "${SHELL:-}" in */zsh) rc="$HOME/.zshrc";; *) rc="$HOME/.bashrc";; esac
    if [[ ! -f "$rc" ]] || ! grep -Fq '# SimpleAI Compute Worker TUI PATH' "$rc"; then
      printf '\n# SimpleAI Compute Worker TUI PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
    fi
  fi
  echo "TUI shortcut: ~/.local/bin/simpleai-tui (open a new terminal)."
else
  echo "Existing unmanaged $launcher left unchanged."
fi

echo "Scheduled updates enabled for $success worker(s); failed: $failed."
if (( failed )); then exit 1; fi
if (( ! success )); then
  echo "No eligible production workers found." >&2
  exit 1
fi
