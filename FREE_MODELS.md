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
8. sorts selected models by registry `priority`, and
9. exposes them as the `free-models` group with `strategy: fallback`.

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

The highest-priority configured free model is tried first. Retryable failures such as 401/403/429 and common 5xx responses fall through to the next credential/model using the proxy's existing fallback behavior.

## Configuration

Optional environment variables:

```bash
AIPROXY_FREE_MODELS_REGISTRY_URL=https://raw.githubusercontent.com/LukaCek/free-ai-models/main/registry.json
AIPROXY_FREE_MODELS_REFRESH_SECONDS=1800
AIPROXY_FREE_MODELS_CACHE_PATH=/app/cache/free-models-registry.json
```

The production Docker Compose configuration mounts both `app.db` and `./cache`, so UI-managed provider credentials and the last-known-good registry survive container recreation.

## Safety properties

- registry data is public configuration only; no provider secrets are stored there,
- provider credentials live only in local environment variables or the local SQLite database,
- dynamic registry providers are stripped before YAML is rendered or persisted,
- an older accidentally persisted registry overlay is automatically removed on startup,
- trial/prototype providers do not enter `free-models` unless the registry explicitly marks them eligible,
- user-configured providers are preserved when the registry refreshes.
