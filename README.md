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
curl -X POST http://localhost:8000/v1/chat/completions -H "Authorization: Bearer ***" -H "Content-Type: application/json" -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"Say hello"}]}'
```

`model` is the **group name** from `config.yml`, not necessarily the upstream model ID. The proxy rewrites it to each selected provider member's `model`.

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
