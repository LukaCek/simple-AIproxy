# Production server setup

Target path on the server:

```bash
/home/ubuntu/docker/simple-AIproxy
```

The deployment workflow expects GitHub Actions secret `DEPLOY_PATH` to point to that directory.

## Files on the server

The server directory should contain:

```text
app.db
docker-compose.yml
.env
config.yml
update.sh
```

`app.db` is persistent runtime data. Do not replace it unless you want to lose API keys/logs.

## 1. docker-compose.yml

Use `server_deployment/docker-compose.yml` as the server version:

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
    mem_limit: 512m
    networks:
      - proxy

networks:
  proxy:
    external: true
```

This keeps admin credentials out of `docker-compose.yml`.

## 2. .env

Create this file on the server only:

```bash
cd ~/docker/simple-AIproxy
nano .env
```

Example:

```env
ADMIN_USERNAME=luka
ADMIN_PASSWORD=CHANGE_THIS_TO_A_NEW_SECRET
```

Important: if the current admin password was pasted into chat/logs, rotate it now.

## 3. config.yml

Start from `server_deployment/config.production.example.yml` and copy it to the server as `config.yml`.

Minimum Codex pool shape:

```yaml
providers:
  - name: codex-a
    url: https://chatgpt.com/backend-api/codex
    models: [gpt-5.5]
    api_mode: codex_responses
    oauth: true
    client_id: app_EMoamEEZ73f0CkXaXp7hrann
    token_url: https://auth.openai.com/oauth/token
    access_token: "PASTE_CODEX_A_ACCESS_TOKEN_HERE"
    refresh_token: "PASTE_CODEX_A_REFRESH_TOKEN_HERE"
    expires_at: ""

  - name: codex-b
    url: https://chatgpt.com/backend-api/codex
    models: [gpt-5.5]
    api_mode: codex_responses
    oauth: true
    client_id: app_EMoamEEZ73f0CkXaXp7hrann
    token_url: https://auth.openai.com/oauth/token
    access_token: "PASTE_CODEX_B_ACCESS_TOKEN_HERE"
    refresh_token: "PASTE_CODEX_B_REFRESH_TOKEN_HERE"
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

Real tokens belong only in the production server `config.yml`, not in git.

## 4. update.sh

Use the existing script:

```bash
chmod +x update.sh
./update.sh
```

## 5. GitHub Actions secrets

Set these repo secrets:

- `DEPLOY_HOST`: `oblak` DNS/IP as reachable from GitHub Actions
- `DEPLOY_USER`: `ubuntu`
- `DEPLOY_PORT`: SSH port, usually `22`
- `DEPLOY_SSH_KEY`: private deploy key
- `DEPLOY_PATH`: `/home/ubuntu/docker/simple-AIproxy`

## 6. Manual smoke test after deploy

On the server:

```bash
cd ~/docker/simple-AIproxy
docker compose pull
docker compose up -d
docker compose logs --tail=100 llm-proxy
```

If the service is behind a reverse proxy, test through the public URL. Otherwise from inside the Docker network/container host, test:

```bash
curl -sS http://127.0.0.1:8000/
```

If your production compose does not publish port `8000`, test via the reverse proxy route instead.
