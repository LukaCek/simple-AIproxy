# Simple AIproxy

A lightweight FastAPI-based LLM API gateway with an OpenAI-compatible facade:

- `POST /v1/chat/completions`
- `GET /v1/models`
- SQLite API-key auth
- YAML-backed providers/groups
- round-robin or fallback routing per group
- OpenAI-compatible provider forwarding, including Ollama
- minimal Responses/Codex adapter for OpenAI Codex OAuth profiles
- request logging and a simple Jinja2/Tailwind admin GUI
- durable background jobs for slow-brain inference

## Why this version exists

The original version mixed provider auth, provider protocol, and client-facing model names. That made the Codex OAuth use-case unreliable. This version separates the important ideas:

- **providers** are real upstream accounts/endpoints (`codex-a`, `codex-b`, `ollama-local`, ...)
- **groups** are model names exposed to clients (`gpt-5.5`, `local-llama`, ...)
- a group can route to multiple providers with `strategy: round_robin` for even usage or `strategy: fallback` for fixed priority fallback
- Codex OAuth profiles use `api_mode: codex_responses`; normal OpenAI-compatible APIs use `api_mode: openai_chat_completions`

## Quick Start (Local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open the admin UI with HTTP Basic auth:

- username: `admin` unless `ADMIN_USERNAME` is set
- password: `admin` unless `ADMIN_PASSWORD` is set

Routes:

- `/admin/keys` — manage proxy API keys
- `/admin/providers` — inspect/add providers
- `/admin/logs` — view request logs
- `/admin/config` — edit YAML config

## Client usage

Create an API key in `/admin/keys`, then call the proxy as an OpenAI-compatible API:

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer API_KEY' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"Say hello"}]}'
```

`model` is the **group name** from `config.yml`, not necessarily the upstream model ID. The proxy rewrites it to each selected provider member's `model`.

For slow local/Ollama models behind Cloudflare or another reverse proxy, use streaming so the edge connection receives chunks instead of waiting silently for the full completion:

```bash
curl -N -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer API_KEY' \
  -d '{"model":"gpt-5.5","stream":true,"messages":[{"role":"user","content":"Say hello"}]}'
```

## Balanced Codex OAuth profiles

`config.yml` contains an example with two Codex profiles:

```yaml
providers:
  - name: codex-a
    url: https://chatgpt.com/backend-api/codex
    models: [gpt-5.5]
    api_mode: codex_responses
    oauth: true
    client_id: app_EMoamEEZ73f0CkXaXp7hrann
    token_url: https://auth.openai.com/oauth/token
    access_token: ""
    refresh_token: ""
    expires_at: ""

  - name: codex-b
    url: https://chatgpt.com/backend-api/codex
    models: [gpt-5.5]
    api_mode: codex_responses
    oauth: true
    client_id: app_EMoamEEZ73f0CkXaXp7hrann
    token_url: https://auth.openai.com/oauth/token
    access_token: ""
    refresh_token: ""
    expires_at: ""

groups:
  gpt-5.5:
    strategy: round_robin
    members:
      - provider: codex-a
        model: gpt-5.5
      - provider: codex-b
        model: gpt-5.5
```

With `strategy: round_robin`, requests rotate `codex-a`, `codex-b`, `codex-a`, `codex-b`, ... while still falling back on retryable upstream failures.

Important: Codex is not a normal `/v1/chat/completions` backend. The proxy exposes `/v1/chat/completions` to clients, then translates simple chat requests to the Codex/Responses backend. This supports basic text requests and a compatibility SSE stream; advanced tool/reasoning behavior may need deeper adapter work later.

## OpenAI-compatible and Ollama providers

Normal providers use:

```yaml
api_mode: openai_chat_completions
url: https://api.example.com/v1
```

Ollama can be configured as:

```yaml
providers:
  - name: ollama-local
    url: http://localhost:11434
    models: [llama3.2]
    api_mode: openai_chat_completions
```

A bare host is normalized to `/v1/chat/completions`.

## Slow-brain background inference

The proxy can enqueue long-running inference jobs without holding the client HTTP
connection open. Jobs are durable in SQLite (`BackgroundJobs`) and the schema is
kept simple so it can be mapped to Postgres later. Existing synchronous
`/v1/chat/completions` behavior is unchanged unless the request includes
`"background": true`.

### Model profile config

Background models live under `model_profiles`, separate from provider `groups`:

```yaml
model_profiles:
  slowbrain-70b:
    type: llamacpp_rpc
    worker_type: llamacpp_rpc_worker
    endpoint: http://llama-main:8080/v1
    health_url: http://llama-main:8080/health
    model: llama-3.3-70b-q2
    timeout_seconds: 21600
    max_parallel_jobs: 1
    max_attempts: 2
    retry_delay_seconds: 60
```

Supported worker types are:

- `llamacpp_rpc_worker` — practical OpenAI-compatible HTTP worker hook for
  llama.cpp server endpoints.
- `airllm_offload_worker` — sidecar/command profile stub; the external worker
  leases jobs and writes partial/final output.
- `accelerate_offload_worker` — sidecar/command profile stub for HF Accelerate
  disk/CPU offload workers.
- `small_prep_worker` — lightweight preparation worker stub.

### API

All endpoints use the same bearer API-key auth as the OpenAI-compatible API.

```bash
curl -sS http://localhost:8000/jobs \
  -H 'Authorization: Bearer ***' \
  -H 'Content-Type: application/json' \
  -d '{"model":"slowbrain-70b","messages":[{"role":"user","content":"Think deeply"}]}'

curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Authorization: Bearer ***' \
  -H 'Content-Type: application/json' \
  -d '{"model":"slowbrain-70b","background":true,"messages":[{"role":"user","content":"Think deeply"}]}'
```

The async chat extension returns immediately:

```json
{"id":"job_...","object":"background.chat.completion","status":"queued"}
```

Status/result endpoints:

- `GET /jobs/{id}`
- `GET /jobs/{id}/logs` — partial output and last error
- `GET /jobs/{id}/result` — final output once succeeded
- `POST /jobs/{id}/cancel`
- `GET /jobs/stats`
- `GET /workers`
- `GET /models`
- `GET /metrics`

### Deployment pattern

Run the existing proxy container as usual. Run slow-brain inference separately:

1. `llama.cpp` RPC/main server on the GPU box:
   ```bash
   llama-server -m /models/llama-3.3-70b-q2.gguf --host 0.0.0.0 --port 8080
   ```
2. Connect the proxy and worker hosts over Tailscale or WireGuard; configure
   `endpoint`/`health_url` to the private name/IP.
3. For x86 disk-offload sidecars, define profiles such as:
   ```yaml
   slowbrain-airllm:
     type: airllm_offload
     worker_type: airllm_offload_worker
     command: python -m workers.airllm_sidecar --model /models/llama-70b --job-id {job_id}
   slowbrain-accelerate:
     type: accelerate_offload
     worker_type: accelerate_offload_worker
     command: accelerate launch workers/accelerate_sidecar.py --model /models/llama-70b --job-id {job_id}
   ```
4. Burn-in checklist: verify `/workers`, check `health_url`, enqueue a tiny job,
   watch `/jobs/{id}/logs`, confirm `/jobs/{id}/result`, then run a full-length
   prompt while monitoring `/jobs/stats` and `/metrics`.

The repository currently provides proxy-side storage, leasing, heartbeat, retry,
and HTTP worker hooks. Start the built-in hook worker with:

```bash
python slowbrain_worker.py --worker-id llama-slot-1
# or process one job for supervised cron/systemd timers:
python slowbrain_worker.py --worker-id llama-slot-1 --once
```

It intentionally does not implement llama.cpp, AirLLM, or Accelerate themselves.

## Docker (Local Testing)

```bash
docker compose up -d --build
```

Access the app at `http://localhost:8000`.

## Tests

```bash
source .venv/bin/activate
pytest -q
```

The tests cover:

- Python 3.13 dependency install compatibility
- Ollama URL normalization
- round-robin routing
- `/v1/chat/completions` proxy behavior
- Codex/Responses adapter response and SSE compatibility

## Production notes

- Do not commit real `access_token`, `refresh_token`, OpenAI-compatible API keys, or admin credentials.
- Prefer deployment-only `config.yml`, mounted secrets, or environment-managed config.
- Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in production.
- The in-memory round-robin counter resets on process restart. If you run multiple worker processes and need exact global balancing, move counters to SQLite/Redis.
