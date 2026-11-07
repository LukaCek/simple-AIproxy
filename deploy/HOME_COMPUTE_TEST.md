# Home CPU Compute Worker test — local, isolated stack

This stack runs the latest checkout's **SimpleAI Proxy + CPU Worker + internal
Caddy HTTPS/WSS** on a single Linux computer. It does **not** modify the real
proxy configuration or database, and only publishes the admin/API port on
`127.0.0.1:18180`.

**Important:** source code is still in draft PR #30; Docker CI is necessary but
does not replace real on-device inference testing. Use a separate directory.

## 1. Verify prerequisites

```bash
docker version
docker compose version
free -h
df -h .
```

A Linux AMD64 or ARM64 Docker host with at least ~4 GiB of spare RAM and several
GiB of free storage is recommended for the 0.8B model and the Docker build.
The llama.cpp build can use significant CPU/time even when runtime limits are
restricted. Use a home computer rather than a production VPS for this test.

## 2. Clone the development branch to a NEW folder

```bash
git clone --branch feat/compute-worker-live-tui --single-branch \
  https://github.com/LukaCek/simple-AIproxy.git \
  ~/simpleai-home-test
cd ~/simpleai-home-test
```

If the directory already exists, do **not** delete it. Inspect it first and
update deliberately rather than overwriting any data.

## 3. Build and start the isolated test stack

```bash
docker compose -f deploy/home-compute-test.compose.yml config --quiet
docker compose -f deploy/home-compute-test.compose.yml up -d --build
docker compose -f deploy/home-compute-test.compose.yml ps
curl -fsS http://127.0.0.1:18180/openapi.json >/dev/null && echo "TEST PROXY OK"
```

Four components are provided: proxy, Caddy gateway, one-time enrollment and
worker; the fifth helper publishes only a local staging CA certificate.
The new source is built locally; no unverified GHCR image is pulled.
Expect the first worker Docker build to take time.

## 4. Check the admin screen

Open <http://127.0.0.1:18180/admin/compute-nodes> on the **same computer**.

Username: `staging-admin`

To read the **staging-only** password without printing it to the chat:

```bash
docker compose -f deploy/home-compute-test.compose.yml exec -T proxy \
  cat /state/admin-password
```

The test password is generated automatically and stored in the isolated
named volume. Don't send it or API keys into a shared chat/log.

Check that **home-test-worker** appears Online. If not, inspect
`docker compose -f deploy/home-compute-test.compose.yml logs --tail=100 worker`.
The local Caddy TLS certificate is trusted by the worker via an exported
*public CA certificate*; Caddy's signing key stays private.

## 5. Choose a tiny model and test inference

On `/admin/compute-nodes` select only `Qwen3.5 0.8B Q4_K_M` and click
**Save & send to workers**. Leave **Manual** placement mode initially.
The worker downloads the pinned GGUF, tests it, then warms it in RAM.
Allow a few minutes; inspect logs and TUI for progress.

Create a **staging-only** API key at
<http://127.0.0.1:18180/admin/keys>.
In a terminal, set it without displaying it:

```bash
read -rsp 'Staging API key: ' API_KEY; echo
curl -sS -N http://127.0.0.1:18180/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"model":"local/qwen35-0.8b-q4","stream":true,"think":false,"max_tokens":64,"aiproxy_visibility":"public","messages":[{"role":"user","content":"Say hello in one short sentence."}]}'
unset API_KEY
```

`"aiproxy_visibility":"public"` is required because the new worker is
untrusted by default. Use only generic test prompts and **do not enable
private prompt access** for this test.

## 6. Open the TUI

In another terminal, from `~/simpleai-home-test`:

```bash
docker compose -f deploy/home-compute-test.compose.yml exec worker python tui.py
```

Shows status, selected models, disk vs RAM, the generating model, throughput,
CPU/RAM/disk usage. Press `q` to exit. For a static view:

```bash
docker compose -f deploy/home-compute-test.compose.yml exec -T worker \
  python tui.py --once
```

## 7. Stop safely

```bash
docker compose -f deploy/home-compute-test.compose.yml down
```

This **does not delete** the isolated Docker volumes. Never run
`down -v` unless you deliberately want to delete all staging state.
Never run Compose commands in your production proxy directory or without the
explicit `-f deploy/home-compute-test.compose.yml` flag.

## Follow-up tests

- Restart worker and proxy independently; verify reconnection.
- Test 2B on a host with sufficient memory and storage.
- Connect a second host via a separate **public HTTPS staging hostname**
  and WSS; this local stack's internal gateway is not an external endpoint.
- Test `local/auto`, latency-based routing and privacy with at least 2 worker
  hosts. A single host cannot prove intelligent scheduling.
