# Production server setup

Current production directory:

```text
/home/ubuntu/docker/simple-AIproxy
```

Set the GitHub Actions secret `DEPLOY_PATH` to that exact directory.

## Persistent files

The server directory should contain:

```text
app.db
cache/
docker-compose.yml
.env
config.yml
update.sh
```

Persistence matters:

- `app.db` contains proxy API keys, completed logs, provider-attempt records,
  UI-managed free-provider credentials, background-job state, and other SQLite
  runtime data.
- `cache/` contains the last-known-good dynamic free-model registry.
- `config.yml` contains user-managed provider/group configuration and may
  contain server-only provider credentials.
- `.env` contains deployment/runtime secrets and environment overrides.

Do not replace `app.db`, `config.yml`, or `.env` during routine deploys.

## 1. Production Docker Compose

Use the tracked `server_deployment/docker-compose.yml`:

```yaml
services:
  llm-proxy:
    image: ghcr.io/lukacek/simple-aiproxy:latest
    container_name: llm-proxy
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./config.yml:/app/config.yml:rw
      - ./app.db:/app/app.db:rw
      - ./cache:/app/cache:rw
    mem_limit: 512m
    networks:
      - proxy

networks:
  proxy:
    external: true
```

The application image starts `full_entrypoint:app`, which composes the base
proxy with true Responses streaming, live logs, Codex usage, audio
transcriptions, and the dynamic free-model registry.

The compose file joins an existing Docker network named `proxy`. Create it
once if your reverse-proxy stack has not already done so:

```bash
docker network create proxy
```

## 2. Server `.env`

Start from `server_deployment/.env.example`:

```bash
cd /home/ubuntu/docker/simple-AIproxy
cp .env.example .env   # only when the example file is present locally
nano .env
```

At minimum, set strong admin credentials:

```env
ADMIN_USERNAME=luka
ADMIN_PASSWORD=CHANGE_THIS_TO_A_LONG_RANDOM_SECRET
```

The same `.env` is also the correct place for optional provider credentials
and runtime tuning. For example:

```env
GEMINI_API_KEY=
GROQ_API_KEY=
MISTRAL_API_KEY=
SAMBANOVA_API_KEY=
OPENROUTER_API_KEY=
CLOUDFLARE_API_KEY=
CLOUDFLARE_ACCOUNT_ID=
NVIDIA_API_KEY=

AIPROXY_PROVIDER_RATE_LIMIT_COOLDOWN_SECONDS=60
AIPROXY_PROVIDER_TIMEOUT_COOLDOWN_SECONDS=30
AIPROXY_PROVIDER_TRANSIENT_COOLDOWN_SECONDS=20
```

Free-provider credentials do not have to live in `.env`: the recommended
interactive path is **Admin → Config → Free provider API keys**, which stores
named credentials in the persistent `app.db`.

Never commit the production `.env`.

## 3. `config.yml`

Start from `server_deployment/config.production.example.yml` and create a
server-only `config.yml`.

Example Codex pool:

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

OAuth tokens may also be created/imported through the Admin Providers UI. Real
tokens belong only in server-side state, never in git.

The `free-models` group is generated dynamically from the registry. Do not add
a static `free-models` group just to make the feature work. Add free-provider
credentials in the Admin Config UI or via the documented environment variables.

## 4. Runtime tuning

Common optional environment variables:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `AIPROXY_UPSTREAM_TIMEOUT_SECONDS` | `3600` | Normal upstream HTTP timeout |
| `AIPROXY_MIN_COMPLETION_TOKENS` | `1024` | Minimum completion-token clamp used by compatible requests |
| `AIPROXY_PROVIDER_RATE_LIMIT_COOLDOWN_SECONDS` | `60` | Fallback cooldown after 429 when no numeric Retry-After is supplied |
| `AIPROXY_PROVIDER_TIMEOUT_COOLDOWN_SECONDS` | `30` | Cooldown after timeout/network failure |
| `AIPROXY_PROVIDER_TRANSIENT_COOLDOWN_SECONDS` | `20` | Cooldown after common transient 5xx/HTML challenge failures |
| `AIPROXY_OLLAMA_DISABLE_THINKING` | `false` | Optional Ollama-specific thinking toggle |
| `AIPROXY_FREE_MODELS_REFRESH_SECONDS` | `1800` | Dynamic registry refresh interval |
| `AIPROXY_FREE_MODELS_CACHE_PATH` | `/app/cache/free-models-registry.json` | Registry cache path |
| `AIPROXY_MAX_AUDIO_BYTES` | `26214400` | Maximum uploaded transcription file size |
| `AIPROXY_AUDIO_TIMEOUT_SECONDS` | `120` | Audio transcription upstream timeout |
| `AIPROXY_PUBLIC_BASE_URL` | unset | Public base URL used where runtime links/callbacks need one |

Provider cooldowns and round-robin counters are in memory and reset when the
container/process restarts.

## 5. First deployment

Make the update script executable:

```bash
chmod +x update.sh
./update.sh
```

The script:

1. creates `cache/` if needed,
2. pulls the latest image,
3. starts/recreates the container,
4. verifies FastAPI from inside the container,
5. verifies the authenticated Admin Config page and its Free provider API keys
   UI, and
6. prunes dangling Docker images.

If any health verification fails, the script exits non-zero and the GitHub
Actions deploy job fails.

## 6. GitHub Actions secrets

Configure:

- `DEPLOY_HOST`: production DNS/IP reachable from GitHub Actions
- `DEPLOY_USER`: `ubuntu`
- `DEPLOY_PORT`: normally `22`
- `DEPLOY_SSH_KEY`: private deployment key
- `DEPLOY_PATH`: `/home/ubuntu/docker/simple-AIproxy`

On each successful push to `main`, CI synchronizes only
`docker-compose.yml` and `update.sh`, then runs `update.sh`. Server-side
secrets/config/database/cache are left untouched.

## 7. Manual verification

```bash
cd /home/ubuntu/docker/simple-AIproxy
docker compose ps
docker compose logs --tail=200 llm-proxy
```

The production compose does not publish port 8000 directly, so public requests
normally arrive through the reverse proxy on the external `proxy` Docker
network. The deployment script performs its health check from inside the
container and does not require a host port mapping.

To verify the exact deployed git revision from the repository side, inspect
`deployment-status.json` on the `deployment-status` branch. A healthy
production run records matching `commit_sha` and `deployed_sha` with
`status: verified`.

## Backup note

Before destructive database/config changes, back up at least:

```bash
cp app.db app.db.backup
cp config.yml config.yml.backup
cp .env .env.backup
```

The SQLite schema is migrated automatically at startup; retaining `app.db`
preserves existing API keys, logs, named provider credentials, and attempt
history across deployments.
