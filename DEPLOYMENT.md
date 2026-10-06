# GitHub Actions CI/CD Deployment Guide

The production workflow is defined in `.github/workflows/deploy.yml`. It runs
on pushes and pull requests targeting `main`, with deployment restricted to
pushes on `main`.

## Pipeline

### 1. Test job

Runs for pushes and pull requests to `main`.

It:

1. checks out the repository,
2. creates a minimal test `config.yml` and `app.db`,
3. installs runtime/test dependencies,
4. runs the configured Ruff checks and the complete pytest suite,
5. starts the application with `docker compose up -d`,
6. waits for `http://localhost:8000/` to become healthy, and
7. tears down the test containers.

A pull request stops here; image publishing and deployment are skipped.

### 2. Build and push

Runs only after the test job succeeds on a push to `main`.

The workflow:

- enables QEMU for ARM64 builds,
- uses Docker Buildx,
- logs in to GHCR using `GITHUB_TOKEN`,
- builds `linux/amd64` and `linux/arm64`, and
- pushes:
  - `ghcr.io/lukacek/simple-aiproxy:latest`
  - `ghcr.io/lukacek/simple-aiproxy:<COMMIT_SHA>`

GitHub Actions cache is used for Docker layers.

### 3. Deploy

Runs only after a successful image build/push.

The workflow first copies these tracked files to the server `DEPLOY_PATH`:

- `server_deployment/docker-compose.yml`
- `server_deployment/update.sh`

It then connects over SSH, runs `./update.sh`, and treats the deployment as
failed if the script does not complete successfully.

`update.sh`:

1. ensures the persistent `cache/` directory exists,
2. pulls the latest image,
3. recreates/updates the container,
4. verifies FastAPI from inside the container,
5. authenticates to `/admin/config` and verifies that the
   **Free provider API keys** UI is present, and
6. prunes dangling Docker images.

### 4. Deployment report

The final `report` job always runs after a `main` push and publishes
`deployment-status.json` on the dedicated `deployment-status` branch.

A verified deployment records:

- the source `commit_sha`,
- the matching `deployed_sha`,
- test/build/deploy results,
- FastAPI health status,
- Admin Config UI verification, and
- a link to the exact workflow run.

This is the authoritative repository-side record that a specific commit was
actually deployed, not merely merged.

## Production server layout

Current production path:

```text
/home/ubuntu/docker/simple-AIproxy
```

Set the GitHub Actions secret `DEPLOY_PATH` to that exact directory.

Persistent/runtime files in that directory are:

```text
app.db
cache/
config.yml
.env
docker-compose.yml
update.sh
```

The workflow overwrites only the tracked deployment copies of
`docker-compose.yml` and `update.sh`. It does **not** replace `app.db`,
`config.yml`, `.env`, or the registry cache.

See `server_deployment/PRODUCTION_SETUP.md` for initial server setup.

## Required GitHub Actions secrets

Configure these under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `DEPLOY_HOST` | Production host/IP reachable from GitHub Actions |
| `DEPLOY_USER` | SSH user, currently `ubuntu` |
| `DEPLOY_PORT` | SSH port, normally `22` |
| `DEPLOY_SSH_KEY` | Private deployment key |
| `DEPLOY_PATH` | `/home/ubuntu/docker/simple-AIproxy` |

## Initial server preparation

```bash
mkdir -p /home/ubuntu/docker/simple-AIproxy/cache
cd /home/ubuntu/docker/simple-AIproxy
```

Copy the starter files once:

```bash
scp server_deployment/docker-compose.yml ubuntu@<SERVER>:/home/ubuntu/docker/simple-AIproxy/
scp server_deployment/update.sh ubuntu@<SERVER>:/home/ubuntu/docker/simple-AIproxy/
scp server_deployment/.env.example ubuntu@<SERVER>:/home/ubuntu/docker/simple-AIproxy/.env
scp server_deployment/config.production.example.yml ubuntu@<SERVER>:/home/ubuntu/docker/simple-AIproxy/config.yml
```

Then create the SQLite bind-mount file before the first container start:

```bash
cd /home/ubuntu/docker/simple-AIproxy
touch app.db
mkdir -p cache
```

This matters because a missing host-side `app.db` bind source can otherwise be
created as a directory by Docker.

Then edit `.env` and `config.yml` on the server. Never commit production
tokens or admin credentials.

Make the update script executable:

```bash
chmod +x update.sh
```

If the GHCR package is private, log Docker into GHCR on the production server
with credentials that can read packages:

```bash
docker login ghcr.io
```

For a public package, an authenticated server-side GHCR login is not normally
required.

## Manual deployment/smoke test

From the production directory:

```bash
./update.sh
```

The script itself performs both FastAPI and authenticated Admin Config checks.
For additional inspection:

```bash
docker compose ps
docker compose logs --tail=200 llm-proxy
```

## SSH key setup

Generate a dedicated deployment key:

```bash
ssh-keygen -t ed25519 -f deploy_key -N ""
```

Install its public key on the server:

```bash
ssh-copy-id -i deploy_key.pub ubuntu@<SERVER>
```

Store the private key as the `DEPLOY_SSH_KEY` GitHub Actions secret.

## Troubleshooting

### Test job fails

Run the same high-level checks locally:

```bash
pip install -r requirements.txt pytest pytest-asyncio ruff
pytest -q
docker compose up -d
curl -f http://localhost:8000/
docker compose down -v
```

The workflow also runs a targeted Ruff command; inspect
`.github/workflows/deploy.yml` for the exact current file list/options.

### ARM64 image build fails

The build runs on an x86 GitHub-hosted runner with QEMU + Buildx. Inspect the
`build-and-push` job rather than assuming ARM64 emulation itself is unsupported.

### Deploy fails

Verify:

```bash
ssh -p <DEPLOY_PORT> <DEPLOY_USER>@<DEPLOY_HOST>
cd /home/ubuntu/docker/simple-AIproxy
docker compose config
./update.sh
```

Because `update.sh` verifies both FastAPI and `/admin/config`, a failure can
mean the container started but the application or admin UI is not actually
healthy. The workflow logs will show the failing step.

### Image pull fails

Check the exact image configured in
`server_deployment/docker-compose.yml` and, if the package is private, verify
the server's GHCR login.

## Notes

- Production currently enforces a `512m` container memory limit.
- `app.db` and `cache/` are bind-mounted and survive container recreation.
- `update.sh` timestamps use the production server's configured local
  timezone; they are not explicitly forced to UTC.
- The deployment workflow deliberately does not copy a git-tracked production
  `config.yml` or `.env`, preventing CI from overwriting server-side
  credentials.
