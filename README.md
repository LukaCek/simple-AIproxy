# Simple AIproxy

A lightweight FastAPI-based LLM API gateway with an OpenAI-compatible facade:

- `POST /v1/chat/completions`
- `GET /v1/models`
- SQLite API-key auth
- YAML-backed providers/groups
- round-robin or fallback routing per group
- OpenAI-compatible provider forwarding, including Ollama
- minimal Responses/Codex adapter for OpenAI Codex OAuth profiles
- request logging, live in-flight request visibility, provider-attempt tracing, and a simple Jinja2/Tailwind admin GUI
- dynamic `free-models` fallback routing from the public registry, including limit-aware preflight checks
- temporary provider/model cooldowns after rate limits, timeouts, and transient upstream failures
- OpenAI-compatible speech-to-text proxying at `POST /v1/audio/transcriptions`
- durable background jobs for slow-brain inference
- optional ntfy alerts when a provider disconnects or recovers

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
uvicorn full_entrypoint:app --host 0.0.0.0 --port 8000
```

Open the admin UI with HTTP Basic auth:

- username: `admin` unless `ADMIN_USERNAME` is set
- password: `admin` unless `ADMIN_PASSWORD` is set

Important: use `full_entrypoint:app` for normal local/production behavior. Running
`main:app` directly only loads the base compatibility layer and omits production
extensions such as the dynamic free-model registry, true Responses streaming,
live logs, Codex usage routes, and audio transcription.

Admin routes:

- `/admin/keys` — manage proxy API keys
- `/admin/providers` — inspect/add/test providers and Codex OAuth profiles
- `/admin/logs` — live + completed request logs
- `/admin/config` — edit YAML config and manage free-provider credentials
- `/admin/codex-usage` — inspect live limits for every Codex OAuth profile
- `/admin/playground` — test configured models/providers from the browser

## Public API surface

Bearer-authenticated client endpoints:

- `POST /v1/chat/completions` — OpenAI-compatible chat facade, including
  streaming and background-job handoff.
- `GET /v1/models` — OpenAI-style model/group listing.
- `POST /v1/audio/transcriptions` — OpenAI-compatible speech-to-text proxy.
- `GET /v1/codex/usage` — live Codex subscription/rate-limit information for
  configured Codex OAuth profiles.
- `POST /jobs` — enqueue a durable background inference job.
- `GET /jobs/{id}`, `/jobs/{id}/logs`, `/jobs/{id}/result` — job state,
  partial logs, and final result.
- `POST /jobs/{id}/cancel` — cancel a queued/running background job.
- `GET /jobs/stats`, `GET /workers`, `GET /models`, and `GET /metrics`
  — background-worker/profile/metrics endpoints.

The Admin UI uses HTTP Basic auth, while the client API uses proxy bearer keys
created under `/admin/keys`.

## ntfy provider alerts

The proxy can periodically test every configured provider and publish one ntfy
alert after repeated failures. It sends no duplicate alerts while the provider
remains down, and can send a recovery notification when it works again.

Enable it in `config.yml`:

```yaml
notifications:
  ntfy:
    enabled: true
    url: https://ntfy.cekluka.com/aiproxy
    username: ""
    password: ""
    check_interval_seconds: 300
    failure_threshold: 2
    notify_recovery: true
```

For protected topics, fill `username` and `password`, or set
`AIPROXY_NTFY_TOKEN`. You can instead set the complete topic URL with
`AIPROXY_NTFY_URL`; this overrides the configured URL. Provider
checks use the first configured model and the normal provider adapter, so choose
an interval appropriate for providers that charge per request.

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

Codex providers use `api_mode: codex_responses`. A minimal two-profile setup is:

```yaml
providers:
  - name: codex-a
    url: https://chatgpt.com/backend-api/codex
    models: [gpt-5.6-sol]
    api_mode: codex_responses
    oauth: true
    client_id: app_EMoamEEZ73f0CkXaXp7hrann
    token_url: https://auth.openai.com/oauth/token
    access_token: ""
    refresh_token: ""
    expires_at: ""

  - name: codex-b
    url: https://chatgpt.com/backend-api/codex
    models: [gpt-5.6-sol]
    api_mode: codex_responses
    oauth: true
    client_id: app_EMoamEEZ73f0CkXaXp7hrann
    token_url: https://auth.openai.com/oauth/token
    access_token: ""
    refresh_token: ""
    expires_at: ""
```

The production Codex entrypoint automatically exposes every configured Codex
profile through round-robin model pools. The built-in selectable order is:

```text
gpt-5.6-sol
gpt-5.6-terra
gpt-5.6-luna
gpt-5.5
```

It also creates a client-facing `codex` group that points at the preferred
default model (currently `gpt-5.6-sol`). You therefore do not need to duplicate
all of those groups manually in YAML. Existing explicit groups are preserved and
the Codex members are added without duplication.

Override the selectable list/default at runtime when needed:

```bash
CODEX_DEFAULT_MODEL=gpt-5.6-sol
CODEX_MODELS=gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna,gpt-5.5
```

With multiple Codex profiles, each generated model group uses
`strategy: round_robin`, so requests rotate across the profiles while still
falling back on retryable upstream failures.

Important: Codex is not a normal `/v1/chat/completions` backend. The proxy exposes
`/v1/chat/completions` to clients and translates requests to the Responses
backend. The production adapter supports normal text, multimodal chat content
conversion, Chat Completions tool definitions/tool choice, assistant tool calls,
tool outputs, and streaming function-call deltas. Provider-specific reasoning
fields are not assumed to be portable: if an OpenAI-compatible provider rejects
historical assistant `reasoning_content`, the proxy retries once with that
unsupported field removed while preserving tool-call structure.

### Live Codex usage API

`GET /v1/codex/usage` returns the same live OpenAI subscription limits as the
admin Codex Usage page, but for all configured Codex profiles in one response.
It uses the normal proxy bearer-key authentication. The results are fetched
from OpenAI for each request and are not written to the proxy database or used
to derive historical usage.

```bash
curl -sS http://localhost:8000/v1/codex/usage \
  -H 'Authorization: Bearer API_KEY'
```

One unavailable profile is returned as an error item while healthy profiles
remain available. Responses include `Cache-Control: no-store` and never include
OAuth tokens or ChatGPT account IDs.

The included `omarchy_agent_usage.py` collector converts this response into the
record consumed by Omarchy's Agents panel. Its configuration supports either a
direct `apiKey`, the `AIPROXY_API_KEY` environment variable, or a reference to
an existing dotenv credential:

```json
{
  "baseUrl": "https://proxy.example.com",
  "apiKeyEnvFile": "~/.config/example/client.env",
  "apiKeyEnvName": "AI_API_KEY",
  "labels": {
    "codex-a": "First account",
    "codex-b": "Second account"
  }
}
```

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


## Fallback reliability and observability

Fallback groups are priority-ordered unless the group uses `round_robin`. Before
dispatch, registry-managed free models carry their published `context_tokens`
and `free_limits` metadata into the resolved endpoint. The proxy estimates
request size and skips a provider when the request is clearly above its published
TPM or context limit. The estimate intentionally uses a safety margin, so this is
a routing guard rather than an exact tokenizer.

Retry/fallback behavior includes:

- `413` falls through to the next provider/model because it can be specific to
  one model's token/rate limit.
- a compatibility `400` for unsupported historical `reasoning_content` is
  sanitized and retried once; unrelated/malformed `400` responses are returned
  to the client instead of blindly falling through.
- `429` falls back and places that provider+model on a temporary cooldown.
  Numeric `Retry-After` is respected when present.
- timeouts/network failures and common transient `5xx` responses also use short
  cooldowns so subsequent requests do not immediately hit the same unhealthy
  endpoint.
- successful requests clear stale cooldown state. `413` deliberately does not
  create a cooldown.

Cooldown state is in memory and therefore resets when the process restarts.

Every client request also gets a shared `request_id`. Individual provider
decisions are persisted in SQLite `ProviderAttempts` with an attempt number,
provider/model, status, timing, and action such as `preflight_skip`,
`cooldown_skip`, `retry_sanitized`, `fallback`, or `success`. The final
row in `Logs` stores the same `request_id`, which makes a fallback chain
correlatable with the client-visible request.

Human-readable `Logs.output` is intentionally separate from wire-protocol
capture: reasoning-only, role-only, and finish-only SSE chunks are ignored, while
tool-call-only responses are summarized instead of dumping raw protocol JSON.

Relevant tuning variables:

```bash
AIPROXY_UPSTREAM_TIMEOUT_SECONDS=3600
AIPROXY_MIN_COMPLETION_TOKENS=1024
AIPROXY_PROVIDER_RATE_LIMIT_COOLDOWN_SECONDS=60
AIPROXY_PROVIDER_TIMEOUT_COOLDOWN_SECONDS=30
AIPROXY_PROVIDER_TRANSIENT_COOLDOWN_SECONDS=20
AIPROXY_OLLAMA_DISABLE_THINKING=false
```

See [FREE_MODELS.md](FREE_MODELS.md) for registry-specific behavior.

## Audio transcription

Providers with `api_mode: openai_audio_transcriptions` are exposed through the
standard OpenAI-compatible endpoint:

```bash
curl -sS http://localhost:8000/v1/audio/transcriptions \
  -H 'Authorization: Bearer API_KEY' \
  -F 'file=@sample.webm' \
  -F 'model=whisper-large-v3-turbo'
```

The proxy accepts the common `language`, `prompt`, `response_format`, and
`temperature` form fields and falls back across transcription-capable
providers on retryable upstream failures. Server-side limits can be tuned with
`AIPROXY_MAX_AUDIO_BYTES` (default 25 MiB) and
`AIPROXY_AUDIO_TIMEOUT_SECONDS` (default 120 seconds).

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

The test suite covers the base proxy and production extensions, including:

- Ollama/OpenAI-compatible URL normalization and routing
- round-robin and fallback behavior
- Codex/Responses text, multimodal, tool-call, and true SSE compatibility
- dynamic free-model registry refresh/credential behavior
- `reasoning_content` compatibility retry and `413` fallback
- TPM/context preflight routing
- provider-attempt tracing and SQLite migrations
- clean streaming output logging
- provider cooldown / `Retry-After` behavior
- audio transcription proxying, live logs, Codex usage, provider monitoring, and background jobs

## Production notes

- Do not commit real `access_token`, `refresh_token`, OpenAI-compatible API keys, or admin credentials.
- Prefer deployment-only `config.yml`, mounted secrets, or environment-managed config.
- Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` in production.
- The in-memory round-robin counter and provider cooldown state reset on process restart. If you run multiple worker processes and need exact global balancing/circuit state, move that shared state to SQLite/Redis.
