#!/usr/bin/env bash
# SimpleAI Compute Worker Linux bootstrap.
# Public installer. One-line setup accepts a short-lived single-use token
# as the second CLI argument (visible in shell history/process listings).
# Older interactive mode is retained for manually provided one-arg commands.
set -Eeuo pipefail
set +x
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }

[[ $# -eq 1 || $# -eq 2 ]] || fail "Usage: curl -fsSL https://YOUR_PROXY/compute/install.sh | bash -s -- https://YOUR_PROXY [ONE_TIME_CODE]"
PROXY_ORIGIN="$1"
case "$PROXY_ORIGIN" in
  https://*) ;;
  http://127.0.0.1:18180|http://localhost:18180) ;;
  *) fail "Proxy must use HTTPS; the only HTTP exception is the isolated 127.0.0.1:18180 home test." ;;
esac
# Reject user-info, whitespace, paths, fragments and other shell/URL ambiguity.
if [[ ! "$PROXY_ORIGIN" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ && \
      ! "$PROXY_ORIGIN" =~ ^http://(127\.0\.0\.1|localhost):18180$ ]]; then
  fail "Expected a bare trusted HTTPS origin, e.g. https://proxy.example.com"
fi
[[ "$(uname -s)" == "Linux" ]] || fail "This installer supports Linux only."
case "$(uname -m)" in
  x86_64|aarch64|arm64) ;;
  *) fail "Only 64-bit AMD64 and ARM64 are supported." ;;
esac
# Docker setup runs on the HOST as the requesting user. Docker never gets
# mounted into the unprivileged worker container. No enrollment code is used
# until all host prerequisites have been verified.
command -v curl >/dev/null 2>&1 ||
  fail "curl is required to run the one-liner itself. Install curl or use a machine with curl."
[[ -z "${DOCKER_HOST:-}" ]] ||
  fail "DOCKER_HOST is set. This installer configures LOCAL Docker only; unset DOCKER_HOST first."

as_root() {
  if (( EUID == 0 )); then
    "$@"
  else
    command -v sudo >/dev/null 2>&1 ||
      fail "This machine needs sudo to set up Docker: $*"
    # sudo reads passwords from the REAL terminal, not from the curl pipe.
    [[ -r /dev/tty && -w /dev/tty ]] ||
      fail "Host setup needs a terminal for sudo approval. Run the one-liner in an interactive terminal."
    sudo -v </dev/tty || fail "sudo authorization denied; host setup not changed further."
    sudo "$@"
  fi
}

install_host_packages() {
  # Only run a package manager if a dependency is actually missing.
  local need_docker=0 need_compose=0 need_python=0
  command -v docker >/dev/null 2>&1 || need_docker=1
  { command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; } || need_compose=1
  command -v python3 >/dev/null 2>&1 || need_python=1
  (( need_docker || need_compose || need_python )) || return 0

  info "Missing host prerequisites: Docker=$need_docker Compose-v2=$need_compose Python3=$need_python"
  info "Installing only missing components; administrator approval may be required."
  local distro='' distro_like='' candidates=()
  if [[ -r /etc/os-release ]]; then
    # Standard Linux distribution release file; not retrieved from the network.
    . /etc/os-release
    distro="${ID:-}"
    distro_like="${ID_LIKE:-}"
  fi
  if [[ "$distro" == "arch" || " $distro_like " == *" arch "* ]]; then
    command -v pacman >/dev/null 2>&1 || fail "Arch Linux package manager pacman is missing."
    # No -Sy: never risk a partial upgrade of an Arch/Omarchy machine.
    candidates=(python docker docker-compose)
    info "Arch/Omarchy: installing packages with pacman (no system-wide upgrade)."
    as_root pacman -S --needed --noconfirm "${candidates[@]}" ||
      fail "pacman failed. Resolve package database / mirror errors before retrying (possibly sudo pacman -Syu)."
  elif [[ "$distro" == "ubuntu" || "$distro" == "debian" ||
          " $distro_like " == *" debian "* ]]; then
    command -v apt-get >/dev/null 2>&1 || fail "apt-get is missing."
    info "Debian/Ubuntu: updating package indexes and installing needed packages."
    as_root apt-get update || fail "Could not update APT package indexes."
    candidates=(ca-certificates curl)
    if (( need_python )); then candidates+=(python3); fi
    if (( need_docker )); then candidates+=(docker.io); fi
    if (( need_compose )); then
      if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
        candidates+=(docker-compose-v2)
      elif apt-cache show docker-compose-plugin >/dev/null 2>&1; then
        candidates+=(docker-compose-plugin)
      else
        info "Compose v2 not present in configured APT repositories; using checksum-verified upstream plugin."
      fi
    fi
    as_root apt-get install -y "${candidates[@]}" ||
      fail "APT Docker installation failed. Resolve repository/package conflicts before retrying."
  elif [[ "$distro" == "fedora" || " $distro_like " == *" fedora "* ]]; then
    command -v dnf >/dev/null 2>&1 || fail "Fedora package manager dnf is missing."
    candidates=()
    if (( need_python )); then candidates+=(python3); fi
    if (( need_docker )); then candidates+=(moby-engine); fi
    if (( need_compose )) && dnf info docker-compose-plugin >/dev/null 2>&1; then
      candidates+=(docker-compose-plugin)
    fi
    if (( ${#candidates[@]} )); then
      info "Fedora: installing ${candidates[*]}."
      as_root dnf install -y "${candidates[@]}" ||
        fail "DNF Docker installation failed. Resolve package repositories before retrying."
    fi
  else
    fail "Unsupported distribution '${distro:-unknown}'. Automatic host bootstrap supports Arch/Omarchy, Debian/Ubuntu and Fedora. Existing Docker installations on other distros still work."
  fi

  command -v docker >/dev/null 2>&1 ||
    fail "Docker CLI is still missing after package installation."
  command -v python3 >/dev/null 2>&1 ||
    fail "Python 3 is still missing after package installation."

  if ! docker compose version >/dev/null 2>&1; then
    # Old Debian/Ubuntu and some Fedora repos lack Compose v2. Download a
    # specific official release and verify its published SHA256 before use.
    # Do not replace any existing, unmanaged user CLI plugin file.
    command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required to verify Compose."
    local version='v5.7.0' arch plugin_dir tmp expected actual
    arch="$(uname -m)"
    case "$arch" in x86_64|aarch64|arm64) ;; *) fail "No Compose plugin for $arch.";; esac
    [[ "$arch" == "arm64" ]] && arch='aarch64'
    plugin_dir="${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins"
    [[ ! -e "$plugin_dir/docker-compose" && ! -L "$plugin_dir/docker-compose" ]] ||
      fail "Existing but unusable Compose plugin in $plugin_dir; resolve manually before retrying."
    mkdir -p -- "$plugin_dir"
    tmp="$(mktemp -d)" || fail "Cannot create temporary Compose download directory."
    local base="https://github.com/docker/compose/releases/download/$version/docker-compose-linux-$arch"
    curl -fsSL --retry 2 "$base" -o "$tmp/docker-compose" ||
      fail "Could not download official Docker Compose plugin ($version)."
    curl -fsSL --retry 2 "$base.sha256" -o "$tmp/checksum" ||
      fail "Could not download Docker Compose SHA256 checksum."
    expected="$(awk 'NR==1 {print $1}' "$tmp/checksum")"
    actual="$(sha256sum "$tmp/docker-compose" | awk '{print $1}')"
    [[ "$expected" =~ ^[a-fA-F0-9]{64}$ && "$expected" == "$actual" ]] ||
      fail "Docker Compose checksum mismatch: refusing to install."
    chmod 755 "$tmp/docker-compose"
    mv -n -- "$tmp/docker-compose" "$plugin_dir/docker-compose"
    rm -rf -- "$tmp"
    info "Installed official, SHA256-verified Docker Compose $version (user CLI plugin)."
  fi
}

start_host_docker() {
  # Only start a service if the daemon is not already reachable as this user.
  if docker info >/dev/null 2>&1; then return 0; fi
  if command -v systemctl >/dev/null 2>&1; then
    info "Starting and enabling Docker Engine so workers survive reboot."
    as_root systemctl enable --now docker.service ||
      fail "Could not start docker.service. Inspect: sudo journalctl -u docker.service -n 80"
  fi
  # A valid daemon running as root is distinct from permission to use it.
  if ! docker info >/dev/null 2>&1; then
    if (( EUID == 0 )); then
      fail "Docker daemon still unavailable as root. Inspect Docker logs; no worker installed."
    fi
    command -v sudo >/dev/null 2>&1 ||
      fail "Docker daemon is not accessible and sudo is unavailable."
    if ! sudo docker info >/dev/null 2>&1; then
      fail "Docker daemon is still unavailable even with sudo. Inspect: sudo systemctl status docker"
    fi
  fi
}

ensure_docker_user_access() {
  if docker info >/dev/null 2>&1; then return 0; fi
  (( EUID != 0 )) || fail "Docker still inaccessible despite running as root."
  local socket_group
  socket_group="$(stat -c %G /var/run/docker.sock 2>/dev/null || :)"
  [[ "$socket_group" == "docker" ]] ||
    fail "Docker is available via sudo but its socket is not owned by docker group. Resolve nonstandard socket permissions manually."
  command -v sg >/dev/null 2>&1 ||
    fail "The sg tool is needed to activate new Docker group access in the current terminal."

  if [[ "${SIMPLEAI_DOCKER_GROUP_REENTRY:-}" == 1 ]]; then
    fail "Docker still inaccessible after group activation. Log out/in or reboot, then rerun the one-liner."
  fi
  info "Docker is running, but your account cannot access the Docker socket."
  info "The Docker group grants root-equivalent privileges. Omarchy may disable this intentionally."
  info "SimpleAI requires non-interactive Docker access for the TUI and its automatic updater."
  [[ -r /dev/tty && -w /dev/tty ]] ||
    fail "Explicit consent is required to grant the Docker group. Use an interactive terminal."
  local answer=''
  IFS= read -r -p "Allow this Linux user passwordless Docker access? Type YES: " answer </dev/tty ||
    fail "Could not read Docker access decision."
  [[ "$answer" == "YES" ]] ||
    fail "Docker group access declined. No worker was installed. Configure rootless Docker or group access manually."

  if ! id -nG | tr ' ' '\n' | grep -qx docker; then
    info "Adding $(id -un) to Docker group (one-time admin action)."
    as_root usermod -aG docker "$(id -un)" ||
      fail "Could not add the current user to Docker group."
  fi

  # Group changes normally require a new login. Re-run the trusted, validated
  # public installer in a NEW Docker-group shell, preserving the one-line UX.
  # No enrollment has happened yet. Quoted arguments were validated above.
  local origin_arg code_arg='' cmd
  printf -v origin_arg '%q' "$PROXY_ORIGIN"
  if [[ -n "${1:-}" ]]; then
    [[ "$1" =~ ^[A-Za-z0-9_-]{32,128}$ ]] ||
      fail "Invalid one-time enrollment code format."
    printf -v code_arg ' %q' "$1"
  fi
  export SIMPLEAI_DOCKER_GROUP_REENTRY=1
  cmd="bash -o pipefail -c 'curl -fsSL ${origin_arg}/compute/install.sh | bash -s -- ${origin_arg}${code_arg}'"
  info "Activating Docker group access in a temporary shell and continuing installation..."
  info "The enrollment code may be visible to local users in process arguments during this step."
  exec sg docker -c "$cmd"
}

install_host_packages
start_host_docker
ensure_docker_user_access "${2:-}"
docker compose version >/dev/null 2>&1 ||
  fail "Docker Compose v2 is still unavailable after setup."
docker info >/dev/null 2>&1 ||
  fail "Docker daemon is still unavailable to this user after setup."

info "SimpleAI Compute Worker — isolated Linux installation"
info "Proxy: $PROXY_ORIGIN"
info "Your existing proxy/worker containers will not be changed."
if [[ $# -eq 2 ]]; then
  ENROLLMENT_CODE="$2"
  # The code is never printed or recorded by the installer, but the initial
  # paste may remain in shell history. It expires or is consumed after use.
else
  [[ -r /dev/tty && -w /dev/tty ]] || fail "Run in an interactive terminal to enter the private setup code."
  IFS= read -r -s -p "Paste the one-time setup code (hidden): " ENROLLMENT_CODE </dev/tty || fail "No setup code provided"
  printf '\n' >/dev/tty
fi
[[ "$ENROLLMENT_CODE" =~ ^[A-Za-z0-9_-]{32,128}$ ]] || fail "Invalid setup code format."

TEMP_DIR="$(mktemp -d)" || fail "Could not prepare secure temporary directory."
cleanup() { rm -rf -- "$TEMP_DIR"; }
trap cleanup EXIT

python3 - "$TEMP_DIR/request.json" "$ENROLLMENT_CODE" <<'PY'
import json, os, sys
with open(sys.argv[1], 'x', encoding='utf-8') as out:
    json.dump({'token': sys.argv[2]}, out)
os.chmod(sys.argv[1], 0o600)
PY
unset ENROLLMENT_CODE
curl --fail --silent --show-error --connect-timeout 10 --max-time 30 \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json' \
  --data-binary "@$TEMP_DIR/request.json" \
  "$PROXY_ORIGIN/compute/bootstrap" >"$TEMP_DIR/bootstrap.json" \
  || fail "Cannot claim setup code. Check proxy address, code expiration and HTTPS certificate."
rm -f -- "$TEMP_DIR/request.json"

# Bootstrap response contains a canonical server-built Compose and enrollment
# credential. Only write files under the user's own unique installation path.
# No eval, shell interpolation or executable content from the response.
INSTALL_DIR="$(python3 - "$TEMP_DIR/bootstrap.json" "$PROXY_ORIGIN" <<'PY'
import json, math, os, pathlib, re, shutil, sys
data = json.load(open(sys.argv[1], encoding='utf-8'))
origin = sys.argv[2]
name = data.get('name', '')
if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
    raise SystemExit('Invalid worker name from proxy')
profile = data.get('profile')
if profile not in ('manual', 'auto_shared', 'auto_max'):
    raise SystemExit('Invalid resource profile from proxy')
local_test = data.get('local_test') is True
if local_test and origin not in ('http://127.0.0.1:18180', 'http://localhost:18180'):
    raise SystemExit('Local test installation must be initiated from the loopback proxy')
if not local_test and not origin.startswith('https://'):
    raise SystemExit('Production installation requires HTTPS')
compose = data.get('compose')
env = data.get('env')
if not isinstance(compose, str) or not isinstance(env, str):
    raise SystemExit('Missing worker setup files from proxy')
if 'compute-worker:' not in compose or 'WORKER_ENROLLMENT_TOKEN=' not in env:
    raise SystemExit('Invalid worker installation definition')
base = pathlib.Path.home() / '.local' / 'share' / 'simpleai-compute'
base.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(base, 0o700)
target = base / name
if target.exists():
    raise SystemExit('Worker directory already exists: ' + str(target) +
                     '\\nChoose a new name. Existing workers are never overwritten.')

if profile != 'manual':
    gib = 1024 ** 3
    def read_mem(key):
        with open('/proc/meminfo', encoding='ascii') as inp:
            for line in inp:
                if line.startswith(key + ':'):
                    return int(line.split()[1]) * 1024
        raise RuntimeError('Cannot detect memory ' + key)
    total = read_mem('MemTotal')
    available = read_mem('MemAvailable')
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)
    root = '/var/lib/docker'
    try:
        import subprocess
        root = subprocess.check_output(
            ['docker', 'info', '--format', '{{.DockerRootDir}}'],
            text=True, timeout=10
        ).strip() or root
    except Exception:
        pass
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = shutil.disk_usage('/').free
    # A stable Docker hard cap is based on TOTAL host RAM, not MemAvailable
    # sampled once while another workload (e.g. a miner) is running.
    # Keep 45% host reserve for Shared or 15% (minimum 2 GiB) for Maximum.
    # This is a *ceiling*, not preallocated RAM. The worker independently
    # checks real-time host MemAvailable before loading each model.
    def auto_ram_gib(total_bytes, shared):
        reserve = max(2 * gib, total_bytes * (0.45 if shared else 0.15))
        return math.floor(max(0, total_bytes - reserve) / gib * 4) / 4

    share = profile == 'auto_shared'
    memory_gb = auto_ram_gib(total, share)
    if memory_gb < 2:
        raise SystemExit('Host has too little physical RAM for a safe auto worker. '
                         'Auto requires at least 2 GiB for the worker plus host reserve.')
    # Auto Maximum can use all physical CPU cores; Linux still schedules other
    # services fairly. Shared deliberately caps CPU at 65% of host capacity.
    cpu_limit = max(0.5, math.floor(cpus * 0.65 * 2) / 2) if share else float(cpus)
    threads = max(1, min(cpus, math.ceil(cpu_limit)))
    # Leave at least 4 GiB on the Docker volume filesystem; disk budget is
    # application-enforced and not an OS-level quota.
    max_disk = max(0, free - 4 * gib)
    disk_gb = min(64, math.floor((max_disk * (0.40 if share else 0.70)) / gib))
    if disk_gb < 2:
        raise SystemExit('Insufficient Docker disk space for a safe auto worker.')
    env += (f'WORKER_MEMORY_LIMIT={memory_gb}g\n'
            f'WORKER_CPU_LIMIT={cpu_limit}\n'
            f'WORKER_THREADS={threads}\n'
            f'WORKER_DISK_BUDGET_GB={disk_gb}\n')
    print(f'Auto {"Shared" if share else "Maximum"}: physical RAM {total / gib:.2f} GiB; '
          f'Docker RAM ceiling {memory_gb:.2f} GiB; '
          f'host reserve {total / gib - memory_gb:.2f} GiB; '
          f'CPU {cpu_limit:g}; threads {threads}; '
          f'model disk budget {disk_gb} GiB', file=sys.stderr)
    if available < (memory_gb + 1) * gib:
        print(f'NOTE: Current available host RAM is only {available / gib:.2f} GiB. '
              'Auto RAM ceilings do not shrink because another process is busy. '
              'The worker will check live available RAM before loading models; '
              'do not run competing heavy workloads without reducing their budgets.',
              file=sys.stderr)

target.mkdir(mode=0o700)
for filename, content in [('docker-compose.yml', compose), ('.env', env)]:
    filepath = target / filename
    fd = os.open(filepath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(content)
print(str(target))
PY
)" || fail "Could not prepare a separate worker directory."

info "Worker configuration: $INSTALL_DIR"
cd "$INSTALL_DIR"

if grep -q 'simpleai-home-test/worker:local' docker-compose.yml; then
  docker image inspect simpleai-home-test/worker:local >/dev/null 2>&1 \
    || fail "Home-test worker image not found. Build the existing test stack first."
  docker network inspect simpleai-home-test_default >/dev/null 2>&1 \
    || fail "Home-test network missing. Start deploy/home-compute-test.compose.yml first."
  docker volume inspect simpleai-home-test_staging-ca >/dev/null 2>&1 \
    || fail "Home-test CA volume missing. Start the original test gateway first."
else
  info "Pulling the multi-architecture Compute Worker image..."
  docker compose pull || fail "Image pull failed. Your files remain in $INSTALL_DIR."
fi

docker compose config --quiet || fail "Invalid generated Compose configuration."
docker compose up -d --no-build || fail "Worker failed to start; inspect: cd $INSTALL_DIR && docker compose logs compute-worker"
info "Waiting for the worker to save its own identity..."
ENROLLED=0
for _attempt in $(seq 1 90); do
  if docker compose exec -T compute-worker test -s /data/identity.json >/dev/null 2>&1; then
    ENROLLED=1
    break
  fi
  sleep 2
done
if [[ "$ENROLLED" != 1 ]]; then
  fail "Enrollment not confirmed. Check 'docker compose logs --tail=80 compute-worker'. Token remains in protected .env for retry until expiry."
fi

# Remove the short-lived token both from the local .env and the running
# container configuration. Permanent worker identity stays in its named volume.
python3 - .env <<'PY'
import os, pathlib, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
new = ''.join(line for line in lines
              if not line.startswith('WORKER_ENROLLMENT_TOKEN='))
tmp = path.with_name('.env.tmp')
fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, 'w', encoding='utf-8') as out:
    out.write(new)
os.replace(tmp, path)
PY
docker compose up -d --no-build --force-recreate >/dev/null \
  || fail "Enrollment saved, but secure token-cleanup restart failed. Inspect Docker logs."

info "Worker installed: $INSTALL_DIR"
info "Identity preserved in its own Docker volume; one-time setup code removed."
if [[ "${SIMPLEAI_DOCKER_GROUP_REENTRY:-}" == 1 ]]; then
  info "IMPORTANT: the new Docker group is active inside this installer session."
  info "Log out and log back in (or reboot once) before using 'simpleai-tui' from"
  info "your original terminal. A reboot is recommended so the persistent"
  info "systemd user auto-update timer also inherits the new Docker group."
fi
info "Check Compute Nodes in the proxy; public requests are allowed by default,"
info "but PRIVATE access remains disabled until explicitly approved."
info "Inspect logs: cd '$INSTALL_DIR' && docker compose logs --tail=80 compute-worker"

# Host-side updater: no Docker socket is mounted into the worker container.
# A user systemd timer survives SSH logouts via one-time linger approval.
# Skip auto updates for the isolated local-test image.
if grep -Fq 'simpleai-home-test/worker:local' docker-compose.yml; then
  info "Local-test image: automatic production-image updates are disabled."
else
  info "Configuring unattended updates (this may request one-time sudo for systemd linger)..."
  if curl -fsSL --connect-timeout 10 --max-time 30 "$PROXY_ORIGIN/compute/auto-update.sh" |
       bash -s -- --install "$(basename "$INSTALL_DIR")" "$PROXY_ORIGIN"; then
    info "Automatic updates active: checks four times per day with rollback on failure."
  else
    info "WARNING: automatic updates are NOT active. Worker still runs normally."
    info "Review systemd setup above; automated updates must be enabled once."
  fi
fi


# Install an unprivileged, one-word launcher. It reads worker folders from the
# existing per-user registry; no credentials or Docker socket are embedded.
install_tui_shortcut() {
  local bin_dir="$HOME/.local/bin"
  local launcher="$bin_dir/simpleai-tui"
  local tmp
  mkdir -p -- "$bin_dir"
  if [[ -e "$launcher" || -L "$launcher" ]]; then
    if ! [[ -f "$launcher" ]] || ! grep -Fq '# SimpleAI Compute Worker managed TUI shortcut' "$launcher"; then
      info "Shortcut $launcher already exists and is not managed by SimpleAI; leaving it unchanged."
      return 1
    fi
  fi
  tmp="$(mktemp "$bin_dir/.simpleai-tui.XXXXXXXX")" || return 1
  cat > "$tmp" <<'SIMPLEAI_TUI_LAUNCHER'
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
  chmod 700 "$tmp"
  mv -f -- "$tmp" "$launcher"

  # A new Ubuntu account may not have ~/.local/bin on PATH until the next
  # login. Add a guarded PATH stanza for *future* Bash/Zsh sessions only.
  if [[ ":$PATH:" != *":$bin_dir:"* ]]; then
    local rc
    case "${SHELL:-}" in
      */zsh) rc="$HOME/.zshrc" ;;
      *) rc="$HOME/.bashrc" ;;
    esac
    if [[ -f "$rc" ]] && ! grep -Fq '# SimpleAI Compute Worker TUI PATH' "$rc"; then
      printf '\n# SimpleAI Compute Worker TUI PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
    elif [[ ! -e "$rc" ]]; then
      printf '# SimpleAI Compute Worker TUI PATH\nexport PATH="$HOME/.local/bin:$PATH"\n' > "$rc"
    fi
    info "Open a NEW shell for the 'simpleai-tui' command; this shell's PATH cannot be changed by an installer."
  fi
  return 0
}

if install_tui_shortcut; then
  info "TUI shortcut installed: simpleai-tui"
  info "Later, from any directory, run 'simpleai-tui' (or 'simpleai-tui <worker-name>')."
  if [[ -r /dev/tty && -w /dev/tty ]]; then
    info "Opening interactive TUI automatically. Press q to return to the shell."
    # The installer was piped to Bash. Reopen the real terminal: Compose and
    # the TUI both require stdin/stdout to be TTYs, not the curl input pipe.
    if ! "$HOME/.local/bin/simpleai-tui" "$(basename "$INSTALL_DIR")" </dev/tty >/dev/tty 2>&1; then
      info "Could not open interactive TUI; try '$HOME/.local/bin/simpleai-tui' manually."
    fi
  else
    info "No interactive terminal available; open the TUI later with 'simpleai-tui'."
  fi
else
  info "Warning: worker registered, but shortcut installation was skipped."
  info "TUI remains available: cd '$INSTALL_DIR' && docker compose exec compute-worker python tui.py"
fi
