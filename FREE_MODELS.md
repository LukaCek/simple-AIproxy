# Dynamic `free-models` pool

SimpleAIProxy builds a live fallback group named `free-models` from the public registry at:

`https://raw.githubusercontent.com/LukaCek/free-ai-models/main/registry.json`

## How it works

On startup the proxy:

1. loads the last-known-good registry from `./cache/free-models-registry.json` when available,
2. fetches the latest registry,
3. validates `schema_version`,
4. keeps only providers marked as eligible for the default free group,
5. loads provider credentials from local environment variables and the local SQLite credential store,
6. resolves provider-specific endpoint values such as Cloudflare Account IDs,
7. creates in-memory `free-registry-*` providers,
8. preserves per-model routing metadata such as `context_tokens`, `free_limits`,
   `capabilities`, and registry status,
9. sorts selected models by registry `priority`, and
10. exposes them as the `free-models` group with `strategy: fallback`.

The registry is refreshed every 30 minutes by default. A failed fetch does not remove the last-known-good pool.

## Credentials

API keys are never read from the public registry and never written to `config.yml`.

The recommended way to manage free-provider credentials is **Admin → Config → Free provider API keys**. Each credential has:

- a registry provider,
- a user-defined name such as `Luka`, `Brother`, or `Backup`,
- an enabled/disabled state,
- the secret API key/token, stored in the local `app.db` SQLite database,
- optional provider-specific non-secret metadata such as a Cloudflare Account ID.

You can add multiple named credentials to the same provider. For a given provider/model, the generated fallback order keeps those credentials adjacent, so retryable failures such as quota/rate-limit responses can fall through to another key before trying the next model/provider.

Cloudflare credentials are account-scoped. Each named Cloudflare credential can therefore use a different Account ID, making setups such as `Luka`, `Brother`, and `Backup` on separate Cloudflare accounts possible.

Environment variables remain supported and are treated as the first credential for their provider:

```bash
GEMINI_API_KEY=...
GROQ_API_KEY=...
MISTRAL_API_KEY=...
SAMBANOVA_API_KEY=...
OPENROUTER_API_KEY=...
CLOUDFLARE_API_KEY=...
CLOUDFLARE_ACCOUNT_ID=...
NVIDIA_API_KEY=...
```

The Admin Config page shows whether each eligible free provider currently has at least one complete usable credential. Secret values are masked in the UI; Cloudflare Account IDs are shown because they are non-secret routing metadata.

## Cloudflare Workers AI

Cloudflare Workers AI uses the OpenAI-compatible endpoint template:

`https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions`

SimpleAIProxy resolves `{CLOUDFLARE_ACCOUNT_ID}` independently for each credential. The registry currently routes confirmed Workers Free models such as GLM 4.7 Flash, Gemma 4 26B, and Nemotron 3 120B. The free allocation is shared across Workers AI usage for the account, so quota errors naturally fall through to the next free-model member.

## NVIDIA NIM

NVIDIA Build exposes OpenAI-compatible free serverless endpoints at `https://integrate.api.nvidia.com/v1`. The registry keeps these entries classified as prototype/trial development access, but they are explicitly permitted as late `free-models` fallbacks. Current registry candidates include GLM 5.2, Nemotron 3 Ultra 550B, and Nemotron 3.5 Lightning 30B.

## Client request

Use the normal OpenAI-compatible endpoint and request `free-models`:

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_SIMPLEAIPROXY_KEY' \
  -d '{"model":"free-models","messages":[{"role":"user","content":"Hello"}]}'
```

The highest-priority configured free model is considered first, but the proxy may
skip it before dispatch when the request is clearly too large for that model's
published TPM or context limit. The preflight token estimate is deliberately
conservative and includes a safety margin; it is a routing guard, not an exact
provider tokenizer.

At runtime:

- `401`, `403`, `408`, `409`, `413`, `425`, `429`, and common
  transient `5xx` responses fall through to the next credential/model.
- `413` is treated as request/model-specific and does **not** put the provider
  on cooldown.
- `429` falls through and temporarily cools that provider+model; a numeric
  `Retry-After` header is respected when present.
- timeouts/network errors and `500/502/503/504` also cause short temporary
  cooldowns, so the next client request can skip the unhealthy member instead
  of immediately retrying it.
- a provider-specific compatibility `400` complaining about historical
  assistant `reasoning_content` is retried once after stripping only that
  unsupported field. Other `400` errors are returned rather than hidden by a
  blind fallback.
- successful requests clear stale cooldown state.

Cooldown state is in memory and resets when the process restarts.

## Configuration

Optional environment variables:

```bash
AIPROXY_FREE_MODELS_REGISTRY_URL=https://raw.githubusercontent.com/LukaCek/free-ai-models/main/registry.json
AIPROXY_FREE_MODELS_REFRESH_SECONDS=1800
AIPROXY_FREE_MODELS_CACHE_PATH=/app/cache/free-models-registry.json

# Provider/model circuit-breaker defaults used by free-models and other groups.
AIPROXY_PROVIDER_RATE_LIMIT_COOLDOWN_SECONDS=60
AIPROXY_PROVIDER_TIMEOUT_COOLDOWN_SECONDS=30
AIPROXY_PROVIDER_TRANSIENT_COOLDOWN_SECONDS=20
```

The production Docker Compose configuration mounts both `app.db` and `./cache`, so UI-managed provider credentials and the last-known-good registry survive container recreation.

## Observability

Every request routed through the normal chat path receives a shared
`request_id`. Each provider decision is written to SQLite
`ProviderAttempts`, including preflight skips, cooldown skips, compatibility
retries, fallbacks, exceptions, and successes. The final `Logs` row stores the
same `request_id`, so one client request can be correlated with every upstream
attempt.

The admin logs page continues to show the client-visible request/result. Its
human-readable output intentionally excludes reasoning-only and protocol-only
SSE chunks; tool-call-only completions are summarized instead of logging raw SSE
JSON.

## Safety properties

- registry data is public configuration only; no provider secrets are stored there,
- provider credentials live only in local environment variables or the local SQLite database,
- dynamic registry providers are stripped before YAML is rendered or persisted,
- an older accidentally persisted registry overlay is automatically removed on startup,
- trial/prototype providers do not enter `free-models` unless the registry explicitly marks them eligible,
- user-configured providers are preserved when the registry refreshes,
- published model limits are used only as routing metadata; the proxy does not
  treat its heuristic token estimate as exact billing/quota accounting,
- provider cooldowns are temporary runtime state and never overwrite registry
  data or stored credentials.
