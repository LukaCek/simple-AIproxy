# SimpleAI Compute Worker

CPU-only GGUF worker for Linux Docker hosts, amd64 or arm64. Requires outbound
HTTPS/WSS, persistent volumes and sufficient local CPU, RAM and disk.

## One-line Linux installation (recommended)

Open /admin/compute-setup on the proxy. Choose a worker name (for example
oblak-vps-1), a publicly reachable HTTPS proxy URL, and Manual limits or an
Auto resource profile. Copy the one-line Linux command into a terminal on the
target machine. The command already contains the 15-minute, single-use
setup code; there is no second prompt. Treat the entire command as a secret:
it may appear in terminal history, clipboard managers or process listings.

Requirements: Linux AMD64 or ARM64; Docker Engine + Compose v2, Python 3,
curl, and outbound HTTPS/WSS. Docker is not installed or configured silently.

The installer creates a new, isolated Compose project in
~/.local/share/simpleai-compute/<worker-name>. It preserves its own model and
identity volumes, clears the enrollment token from the private .env file
after successful registration, and recreates that one container without it.
The worker cannot access the host's Docker socket. Newly enrolled workers
are untrusted for private prompts until approved in the admin dashboard.

### One-word TUI command

After successful enrollment, the Linux installer creates the executable
`~/.local/bin/simpleai-tui` (mode 0700) and **automatically opens** the live
terminal dashboard. Press `q` to leave the TUI; the worker keeps running.

From any directory in a new terminal, run:

```bash
simpleai-tui
```

If multiple workers are installed for the same Linux user, pick one:

```bash
simpleai-tui my-worker
```

For a static status snapshot, use `simpleai-tui --once` (or
`simpleai-tui my-worker --once`). This calls `docker compose exec`
in the worker's existing isolated Compose project. It does **not** start or
stop services and does not need to see worker API keys.

On systems where `~/.local/bin` was not in PATH when setup ran, the
installer adds it to the current user's Bash/Zsh startup file; open a
**new terminal** first. You can always use
`~/.local/bin/simpleai-tui` directly. Existing unmanaged shortcut files
are never overwritten. The installer opens the dashboard only when
a real interactive terminal is available; non-interactive installs skip
this optional step.


**Auto Maximum** sets the worker Docker RAM ceiling to approximately
85% of the target Linux host's total physical RAM (minimum 2 GiB reserved for
the host), and permits all host CPU cores. This is a *hard ceiling*, not RAM
preallocation. It does **not** shrink based on `MemAvailable` at installation:
a running Bitcoin miner or other temporary process cannot permanently limit
the worker to the memory that was free at setup. Before actually loading a
GGUF model, the worker checks live host memory availability and its own cgroup
headroom. If another process is consuming RAM, the worker may wait/fail the
model test until resources become available. Model tests previously marked
failed can be explicitly retried by saving the model policy again.

**Auto Shared** reserves approximately 45% of total RAM (at least 2 GiB) for
other workloads and limits worker CPU to 65%. For machines where a memory-heavy
miner or other services may run concurrently, prefer Auto Shared or Manual:
even an 85% cap does **not** guarantee the host cannot run out of memory if
other containers/processes also consume large amounts. The worker is marked as
a preferred OOM-kill candidate, but that is not a substitute for coordinating
total memory budgets across services.


### Unattended Linux worker updates

New production workers configure a per-user **systemd timer** automatically
during the one-line installation. It checks the trusted GHCR worker `latest`
image four times daily, using Docker **on the host only**. Nothing grants the
worker container access to the host Docker socket. An unattended update runs
only while the worker is idle, preserving model downloads, identity, data and
enrollment policy through the existing named volumes. It verifies the worker
reconnects within 3 minutes and rolls back to the previous image if not.
A previously failed image is skipped until GHCR publishes a different image.
The updater does not modify the proxy or unrelated Compose projects.

The installer may ask for **one-time sudo approval** to run
`loginctl enable-linger USER`, keeping the user's systemd timers active after
the SSH session closes. If the host cannot provide systemd, linger, or Docker
permissions, setup reports that automatic updates are **not enabled** rather
than silently claiming success. On Linux without systemd, configure a
supported host scheduler separately.

For a **previously installed** production worker, add the updater once
without removing the worker's volumes. From the worker's own Compose
directory, using the same public HTTPS proxy origin the worker already
uses:

```bash
ORIGIN="$(sed -n 's/^SIMPLEAI_PROXY_URL=//p' .env)"
curl -fsSL "$ORIGIN/compute/auto-update.sh" | bash -s -- --install "$(basename "$PWD")" "$ORIGIN"
```

To also migrate an older Auto Maximum installation from its outdated,
one-time-free-RAM limit, append `--recalculate-auto-max` to the above
command. This recalculates the maximum cap from physical host RAM, recreates
only that worker's container and checks reconnection; the former limit is
restored if it fails. Use the flag only if the worker was previously created
with **Auto Maximum**, not Manual or Auto Shared. A new auto installation
already uses the corrected formula and does not need migration.

Check the schedule:
`systemctl --user status simpleai-compute-update@WORKER_NAME.timer`.

Review updater activity:
`journalctl --user -u simpleai-compute-update@WORKER_NAME.service -n 60`.

Disable automatic updates without stopping the worker:
`systemctl --user disable --now simpleai-compute-update@WORKER_NAME.timer`.

The host always retains a Docker memory ceiling. Automatic **image**
updates intentionally do **not** silently modify previously chosen CPU/RAM
settings. Major feature changes can still require manual configuration.
If an update begins just as an external request arrives, that request can be
interrupted; the timer checks idle state twice to minimize this risk.

Auto profiles also set a model-storage budget based on free space in Docker's
filesystem (with disk headroom). These are conservative resource budgets, not
guarantees that any particular model will fit. The auto values are stored
in the worker's private `.env` and remain fixed until explicitly changed or
a new installation is performed; they are not dynamically recalculated.

### Test a second worker on the same computer

Update and rebuild the isolated deploy/home-compute-test.compose.yml stack.
Open http://127.0.0.1:18180/admin/compute-setup on that computer. Set a
distinct name, e.g. oblak-vps-1, and use the default internal HTTPS worker
URL. Copy the generated one-line command (which includes its code) to the SAME host.
It uses the existing home-test image, the public staging CA and network,
but gets its own Compose project, model cache and identity. The original
home-test-worker and production services are not changed by this installer.

For an actual VPS or Raspberry Pi use the externally reachable public HTTPS
proxy and the published multiarchitecture worker image, not loopback staging.

## Installation

1. First deploy the proxy version containing PR #24 and publish the worker
   image. A draft PR alone does not make the image or endpoints available.
2. Open `/admin/compute-nodes` as an administrator and click **Generate
   15-minute enrollment token**. Alternatively POST
   `/admin/api/compute/enrollments` with admin Basic auth **and** the header
   `X-SimpleAI-Admin-Action: 1` (required to prevent cross-site writes).
3. On the Linux Docker host, copy `worker/docker-compose.yml` to
   `docker-compose.yml`. **Only edit that one Compose file:** set
   `SIMPLEAI_PROXY_URL` to your external HTTPS proxy and paste a one-time token
   into `WORKER_ENROLLMENT_TOKEN`. Configure `mem_limit` / `memswap_limit`,
   `cpus`, `pids_limit`, `WORKER_THREADS`, and `WORKER_DISK_LIMIT_GB` there.
   Protect the Compose file (e.g. `chmod 600 docker-compose.yml`) since it
   initially contains the one-time enrollment token. The token is ignored once
   the worker identity is stored in the persistent worker-data volume.
4. Run `docker compose pull && docker compose up -d`. Check
   `docker compose logs --tail=100 compute-worker` and confirm the node
   appears as Online in the admin dashboard.
5. Worker defaults to `WORKER_REASONING=on`. The proxy controls reasoning per request: omit `think` (default on), set `"think":true` (on), or `"think":false` (off). The proxy converts this into llama.cpp `chat_template_kwargs.enable_thinking`, so the model does not reload. `reasoning_effort:"none"` also disables thinking. `WORKER_REASONING=auto` uses the model template default.\n6. Install then Load a small model (start with 0.8B on a low-RAM machine)
   from the dashboard. Verify a sample OpenAI-compatible request before
   granting access to private prompts.

Enrollment tokens expire after 15 minutes. Worker identity is stored in a
persistent data volume with permission 0600. Do not copy this volume to another
computer. Revoke nodes from the proxy administration.

## Automatic operation

After first enrollment, the administrator opens `/admin/compute-nodes` in
SimpleAI Proxy and checks the catalog models to make available. **Nothing on
the worker host needs to be configured for individual models.** The proxy
persists the selected list and delivers it over authenticated WSS, including
after reconnecting.

Each worker checks its CPU/RAM limit and disk quota first. Models that pass
preflight are downloaded from the pinned, SHA256-checked catalog, loaded,
given a real short inference test, and reported back as supported/error/
unsupported. The highest-priority supported model is warmed by default.
Other supported models can be selected by `local/<model-id>`; a worker
loads the requested model automatically when routed, without requiring
`install` or `load` from the admin. One model can be resident per CPU worker
at a time; switching models introduces startup latency.

`WORKER_DISK_LIMIT_GB` limits application-level bytes in `/models`. Old
cached GGUF files are evicted in least-recently-used order as required.
Each file is downloaded with a streaming byte cap, checked by SHA256 and
atomically published. The model cache can contain fewer models than have
passed tests; tested capabilities are persisted under `/data` and exposed
to SimpleAI Proxy. Changing the policy triggers tests for new models;
saving the unchanged list again re-runs tests.

**Storage limitation:** Compose has no portable hard disk quota for named
Docker volumes. The worker enforces a bounded *application* quota on model
downloads. This does not limit filesystem growth by other processes or provide
a kernel-enforced quota. Use a dedicated quota-managed filesystem if an
absolute OS-level cap is required. The cache and model downloads count toward
the configured cap; persistent identity/metadata lives separately in `/data`.

## Models

Curated, checksum-verified GGUF entries are in compute_catalog.py.
Install, load, unload, delete through:
POST /admin/api/compute/nodes/{id}/operations
{"action":"install","model":"qwen35-4b-q4"}

Check:
GET /admin/api/compute/operations/{operation_id}

Ready endpoint:
POST /v1/chat/completions
{"model":"local/qwen35-4b-q4","messages":[{"role":"user","content":"Hello"}]}

Privacy: all inference is private by default. Only explicitly trusted
nodes (allow_private=true) may receive private prompts. To send an explicitly
public request to an untrusted node, set "aiproxy_visibility":"public".
The remote host operator can access prompt contents regardless of TLS.

Thinking defaults to ON for compute requests, and the proxy can choose per
request: `"think": false` for a fast answer, `"think": true` for reasoning,
or omit the field to retain the default ON. No model reload is required.
The proxy automatically limits reasoning to one quarter of the specified
`max_tokens` (maximum 256), leaving room for a visible response. When the
caller omits `max_tokens`, the default reasoning budget is 192 tokens. The
caller can override with `"reasoning_budget_tokens": 128`; that value must
be a nonnegative integer smaller than the completion limit if specified.
For example, `{"think":true,"max_tokens":512}` allows up to 128 reasoning
tokens, leaving at least 384 generated tokens for an answer. Limits are
upper bounds, not guarantees of a correct or nonempty response.

Deleting a model also clears its per-model download cache to reclaim disk space.
Never copy `/data/identity.json` to another machine; generate a new enrollment
for each physical computer. If a node is revoked, its stored credentials cannot
be reused.

Unprivileged container with no Docker socket, no exposed ports, configurable
Docker CPU/memory limits and restricted model operations.

## Local live terminal dashboard (TUI)

After starting a worker, open a terminal on the machine running Docker:

```bash
docker compose exec compute-worker python tui.py
```

The dashboard runs **inside the existing container**; it does not start a
second inference worker or require a separate port. Press **q** to quit.
To print one snapshot without an interactive TTY:

```bash
docker compose exec -T compute-worker python tui.py --once
```

The dashboard updates every second and shows:
- Current connection status to the proxy and the live phase (idle, checking
  disk/downloading, loading, benchmarking, generating or disconnected).
- The model currently active for inference and elapsed job time.
- All catalog models, which are approved by policy, tested capability,
  saved GGUF files (disk), current model resident in **RAM**, and which model
  is actively generating right now.
- Actual measured generation tokens/second, when reported by llama.cpp.
- Model-process CPU usage (**Inference CPU**) separate from overall container CPU
  (**Container CPU**, which includes TUI rendering). Both are normalized
  against the configured Docker CPU quota.
- llama-server resident RAM (RSS) when the process exists, container cgroup
  RAM and model storage quota. A recent inference-CPU/memory history is shown
  on wide terminals. While the model is loading, TUI shows its current
  process RSS and elapsed time; exact load percentage is unavailable in our
  pinned *direct* llama-server mode. It would be incorrect to infer a percent
  from mmap/page-cache usage, so no fabricated loading % is displayed.

Worker status is written atomically to `/data/worker-runtime-status.json`
with permissions 0600. The TUI is **read-only** and polls only this local
snapshot, the local model directory and Linux cgroup counters. It never
opens an API connection, executes shell commands, downloads models or reads
the worker's identity file. **Prompts, API keys and enrollment credentials are
never written into the snapshot**.

If the worker process stops, the TUI labels the snapshot stale after 45
seconds rather than pretending the old job is still running. CPU/memory
gauges use the container's cgroup; they are *not host-wide* utilization.
The disk budget is an application-enforced quota, not a filesystem limit.

If your terminal is too narrow, the resource cards collapse to a compact
table. The TUI requires a real interactive terminal for live mode; use
`--once` when piping output to another command.

## Proxy request logs and worker event audit

- Normal Compute Worker chat calls appear in **Request Logs** with their
  selected provider/model, status and timing. **Provider Attempts** records
  fallback, errors and `privacy_skip` (private prompts never forwarded to
  untrusted nodes).
- Streaming calls are logged **after the stream completes or fails**, not when
  HTTP 200 headers are first created. Interrupted streams are recorded as
  `stream_error` (502 in the audit record) even if the client has already
  received HTTP 200 headers.
- On `/admin/compute-nodes`, **Compute worker events** shows connect/disconnect,
  policy updates, model self-test statuses, model load/unload, manual operations
  and trust/revocation changes. The admin-only read endpoint is
  `GET /admin/api/compute/events?limit=100`; filter with `node_id=...`.
- The event audit stores **only structured metadata**, not prompt contents,
  model outputs, WSS payloads, node credentials or API keys. Heartbeats are
  not logged. Retention is at most 30 days and 1000 events per node.
- Raw `llama-server` output is not collected by this audit; the worker's
  Docker stdout captures disconnect errors. Worker model test details remain
  available through the admin node capabilities status.
- **Version updates:** the worker image is currently updated on its own host
  with `docker compose pull && docker compose up -d`. The admin proxy does
  *not* have Docker daemon privileges, and must not be granted a host Docker
  socket just to offer remote updates. A remote-update feature requires a
  separate opt-in, strongly restricted host update service, image verification,
  explicit host-owner consent and rollback.

## Pre-production acceptance checks

- All proxy and worker GitHub CI tests pass, including both amd64 and arm64
  container builds.
- Public HTTPS endpoint and Cloudflare proxy accept outbound WSS upgrades;
  node stays connected and recovers after proxy/worker restart.
- Run a real SHA256-verified model download, load and chat-completion test on
  the target ARM VPS, then unload and delete it, checking released disk space.
- Repeat enrollment and inference from a separate Docker host.
- Confirm friend workers cannot receive private prompts by default. Test
  explicit Public requests, revocation, and disabling Private access.
- Keep the PR unmerged until those checks are complete, as merging into main
  triggers the existing production deployment workflow.
