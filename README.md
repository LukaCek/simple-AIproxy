# Simple AIproxy

A lightweight FastAPI-based LLM API gateway and proxy with OpenAI-compatible `/v1/chat/completions`, YAML-backed provider groups, watchdog live reload, SQLite API key auth, request logging, and a simple Jinja2/Tailwind admin GUI.

## Quick Start (Local)

1. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Run the app:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

3. Use the admin UI:
   - `/admin/keys` — Manage API keys
   - `/admin/logs` — View request logs
   - `/admin/config` — Edit YAML configuration

## Docker (Local Testing)

Build and run with Docker Compose:
```bash
docker compose up -d
```

Access the app at `http://localhost:8000`.

## Production Deployment

For deployment on Oracle ARM servers (linux/arm64) with 2GB RAM, see [DEPLOYMENT.md](DEPLOYMENT.md) for:
- GitHub Actions CI/CD workflow setup
- SSH secrets configuration
- Server preparation and manual deployment steps
- Automated build and push to GitHub Container Registry (GHCR)

## Architecture Notes

- **Config Management**: `config.yml` defines model groups and provider endpoints with priority-based fallback routing
- **Database**: `app.db` (SQLite) stores API keys and request logs
- **Live Reload**: `watchdog` monitors `config.yml` and reloads in-memory config on disk changes
- **Streaming**: `httpx.AsyncClient` forwards streaming responses from backend providers
- **Auth**: Bearer token validation against API keys in SQLite
- **Logging**: Asynchronous request logging via `BackgroundTasks` (non-blocking)
