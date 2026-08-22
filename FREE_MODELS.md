# Dynamic `free-models` pool

SimpleAIProxy can build a live fallback group named `free-models` from the public registry at:

`https://raw.githubusercontent.com/LukaCek/free-ai-models/main/registry.json`

## How it works

On startup the proxy:

1. loads the last-known-good registry from `./cache/free-models-registry.json` when available,
2. fetches the latest registry,
3. validates `schema_version`,
4. keeps only providers marked as eligible for the default free group,
5. checks whether the provider's required local API-key environment variable exists,
6. creates in-memory `free-registry-*` providers,
7. sorts selected models by registry `priority`, and
8. exposes them as the `free-models` group with `strategy: fallback`.

The registry is refreshed every 30 minutes by default. A failed fetch does not remove the last-known-good pool.

## Credentials

API keys are never read from the public registry. The registry contains only the name of the environment variable that should hold each key.

Set whichever providers you want to use in the deployment `.env` file:

```bash
GEMINI_API_KEY=...
GROQ_API_KEY=...
MISTRAL_API_KEY=...
SAMBANOVA_API_KEY=...
OPENROUTER_API_KEY=...
NVIDIA_API_KEY=...
```

Only providers with a non-empty local key become active. NVIDIA is tracked by the registry but is currently excluded from the automatic group because its hosted free access is classified as prototype/API-trial access.

## Client request

Use the normal OpenAI-compatible endpoint and request `free-models`:

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_SIMPLEAIPROXY_KEY' \
  -d '{"model":"free-models","messages":[{"role":"user","content":"Hello"}]}'
```

The first available provider is tried first. Retryable failures such as 401/403/429 and common 5xx responses fall through to the next registry member using the proxy's existing fallback behavior.

## Configuration

Optional environment variables:

```bash
AIPROXY_FREE_MODELS_REGISTRY_URL=https://raw.githubusercontent.com/LukaCek/free-ai-models/main/registry.json
AIPROXY_FREE_MODELS_REFRESH_SECONDS=1800
AIPROXY_FREE_MODELS_CACHE_PATH=/app/cache/free-models-registry.json
```

The Docker Compose configuration mounts `./cache` into `/app/cache` so the last-known-good registry survives container recreation.

## Safety properties

- registry data is configuration only; no provider secrets are stored there,
- provider credentials remain local environment variables,
- registry-generated providers exist only in memory and are never written into `config.yml`,
- trial/prototype providers do not enter `free-models` unless the registry explicitly marks them eligible,
- user-configured providers are preserved when the registry refreshes.
