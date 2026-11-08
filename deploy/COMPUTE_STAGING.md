# Isolated CPU worker staging on a production VPS

This is a separate Compose project, `simpleai-compute-staging`. It runs a staging
SimpleAI Proxy plus a CPU worker on the **same VPS** without replacing the
existing production proxy, database, config or Cloudflare routes.

Files:
- `deploy/compute-staging.compose.yml`: all four services in one Compose project.
- `deploy/compute-staging.config.yml`: empty, isolated provider configuration.
- `deploy/compute-staging.env.example`: template for local non-committed settings.

Services: `llm-proxy`, `tls-gateway` (internal Caddy TLS/WSS),
`ca-export` (public CA only) and `compute-worker`.
The proxy binds ONLY to VPS loopback at `127.0.0.1:18080`; the worker's
gateway does not publish any port. Named volumes and `staging-runtime/app.db`
belong exclusively to this test stack.

**Important:** This setup exercises internal TLS/WSS. It does **not** prove
Cloudflare WSS works; that is a separate later release gate.

## 1. Prepare in a NEW checkout on the VPS

Never switch branches or build inside the working directory used for production.
Create a second checkout instead (use your configured GitHub authentication):

```bash
git clone --branch feat/compute-worker-v1 --single-branch https://github.com/LukaCek/simple-AIproxy.git "$HOME/simpleai-compute-staging"
cd "$HOME/simpleai-compute-staging"
free -h && df -h
mkdir -p staging-runtime
[ -f staging-runtime/app.db ] || install -m 600 /dev/null staging-runtime/app.db
cp deploy/compute-staging.env.example deploy/compute-staging.env
chmod 600 deploy/compute-staging.env
openssl rand -hex 24
```

Edit `deploy/compute-staging.env` and replace the placeholder
`STAGING_ADMIN_PASSWORD` with a unique random password.

## 2. Start staging proxy and internal TLS gateway

```bash
docker compose --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml config --quiet
docker compose --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml up -d --build llm-proxy tls-gateway
curl -fsS http://127.0.0.1:18080/openapi.json >/dev/null && echo "Staging proxy OK"
```

## 3. Build worker BEFORE creating enrollment token

```bash
docker compose --profile worker --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml build compute-worker
```

The worker image builds `llama.cpp` from source, so this step can be
CPU-intensive and take longer than the 15-minute enrollment token lifetime.
Prefer off-peak VPS load. Worker runtime limits do not limit build-time CPU.

## 4. Register worker

To view staging admin UI safely from a different computer, use an SSH tunnel:

```bash
ssh -L 18080:127.0.0.1:18080 YOUR_USER@YOUR_VPS
```

Open `http://127.0.0.1:18080/admin/compute-nodes` on that computer and sign
in with the **staging** admin credentials. Click "Generate 15-minute enrollment
token", then paste it into `STAGING_WORKER_ENROLLMENT_TOKEN=` in
`deploy/compute-staging.env` on the VPS. Run:

```bash
docker compose --profile worker --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml up -d --no-build compute-worker
docker compose --profile worker --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml logs --tail=80 compute-worker
```

The staging dashboard should show `vps-staging-worker` as Online. Each worker
has its own persistent credentials. Once enrolled, remove the enrollment token
from the environment file and recreate the worker when convenient.

## 5. Smoke test

In the staging admin dashboard install and load `qwen35-0.8b-q4`.
Create a separate test API key in `/admin/keys` and call:

```bash
curl -sS http://127.0.0.1:18080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_STAGING_API_KEY' \
  -d '{"model":"local/qwen35-0.8b-q4","aiproxy_visibility":"public","messages":[{"role":"user","content":"Pozdrav"}]}'
```

Friend or unknown workers are **untrusted by default**. Use only explicitly
public test prompts; never enable private-prompt access during initial tests.

## Status, stop, cleanup

```bash
docker compose --profile worker --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml ps
docker compose --profile worker --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml logs --tail=100
docker compose --profile worker --env-file deploy/compute-staging.env -f deploy/compute-staging.compose.yml down
```

`down` leaves staging named volumes intact. Never run `down` or `down -v`
without specifying the staging Compose file on a VPS that also runs production.
