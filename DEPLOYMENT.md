# GitHub Actions CI/CD Deployment Guide

This directory contains the GitHub Actions workflow and server deployment scripts for the FastAPI LLM Proxy.

## Workflow Overview

### 1. Test Job (`test`)
- Triggers on every push and pull request to `main`
- Creates a minimal `config.yml` for testing
- Starts the application using `docker compose up -d`
- Performs a health check by pinging `localhost:8000`
- Cleans up resources after completion

### 2. Build and Push Job (`build-and-push`)
- **Depends on:** `test` job must pass
- **Triggers:** Only on pushes to `main` (not on PRs)
- Builds Docker images for both `linux/amd64` and `linux/arm64`
- Pushes to GitHub Container Registry (GHCR) with:
  - `:latest` tag (always points to latest main branch build)
  - `:${COMMIT_SHA}` tag (immutable reference to specific commit)
- Uses GitHub Actions cache (type=gha) for faster builds

### 3. Deploy Job (`deploy`)
- **Depends on:** `build-and-push` job must complete
- **Triggers:** Only on pushes to `main`
- Connects to Oracle ARM server via SSH
- Executes `./update.sh` to pull latest image and restart container

## Production server files

For the current production layout at `/home/ubuntu/docker/simple-AIproxy`, use:

- `server_deployment/docker-compose.yml`
- `server_deployment/.env.example` copied to server as `.env`
- `server_deployment/config.production.example.yml` copied to server as `config.yml`
- `server_deployment/update.sh`

Detailed server-side instructions are in `server_deployment/PRODUCTION_SETUP.md`.

## Setup Instructions

### Step 1: Set GitHub Secrets

In your GitHub repository, add the following secrets (go to **Settings > Secrets and variables > Actions**):

| Secret Name | Description | Example |
| --- | --- | --- |
| `DEPLOY_HOST` | IP address or hostname of Oracle ARM server | `192.0.2.1` or `oracle.example.com` |
| `DEPLOY_USER` | SSH username | `ubuntu` or `root` |
| `DEPLOY_PORT` | SSH port (default is 22) | `22` |
| `DEPLOY_SSH_KEY` | Private SSH key (use multiline format) | `-----BEGIN OPENSSH PRIVATE KEY-----...` |
| `DEPLOY_PATH` | Absolute path on server where `docker-compose.yml` and `update.sh` reside | `/home/ubuntu/llm-proxy` |

### Step 2: Prepare Oracle Server

1. Create deployment directory:
   ```bash
   mkdir -p /home/ubuntu/llm-proxy
   cd /home/ubuntu/llm-proxy
   ```

2. Copy files from `server_deployment/`:
   ```bash
   # Copy docker-compose.yml
   scp server_deployment/docker-compose.yml ubuntu@<SERVER_IP>:/home/ubuntu/llm-proxy/
   
   # Copy update.sh
   scp server_deployment/update.sh ubuntu@<SERVER_IP>:/home/ubuntu/llm-proxy/
   ```

3. On the server, edit `docker-compose.yml` and replace `YOUR_GITHUB_USERNAME` with your actual GitHub username.

4. Ensure Docker and Docker Compose are installed:
   ```bash
   docker --version
   docker compose version
   ```

5. Log in to GitHub Container Registry (requires a Personal Access Token with `read:packages` scope):
   ```bash
   docker login ghcr.io
   # Username: your_github_username
   # Password: your_personal_access_token
   ```

6. Create a sample `config.yml` and `app.db` placeholder:
   ```bash
   touch config.yml app.db
   ```

### Step 3: Test Manually

Before relying on automation, test the update script manually:

```bash
cd /home/ubuntu/llm-proxy
./update.sh
```

### Step 4: Push to Main Branch

Once everything is set up, push a commit to the `main` branch:

```bash
git push origin main
```

The GitHub Actions workflow will:
1. Run tests
2. Build multi-platform Docker images
3. Push to GHCR
4. Deploy to your Oracle server

## SSH Key Setup

### Generate SSH Key (on your local machine):
```bash
ssh-keygen -t ed25519 -f deploy_key -N ""
```

### Add Public Key to Server:
```bash
ssh-copy-id -i deploy_key.pub ubuntu@<SERVER_IP>
```

### Add Private Key to GitHub Secrets:
1. Copy the contents of `deploy_key` (private key)
2. In GitHub repo settings, add a secret named `DEPLOY_SSH_KEY`
3. Paste the entire private key content (including `-----BEGIN...` and `-----END...` lines)

## Monitoring

### View Workflow Runs
- Go to **Actions** tab in your GitHub repository
- Click on "Deploy FastAPI LLM Proxy" to see recent runs

### Check Server Logs
```bash
ssh ubuntu@<SERVER_IP>
cd /home/ubuntu/llm-proxy
docker compose logs -f llm-proxy
```

## Troubleshooting

### Workflow Fails at Test Stage
- Check that Docker and Docker Compose are installed on the GitHub Actions runner (they are by default)
- Verify the `Dockerfile` is correct and can build successfully

### Build Job Fails for ARM64
- This is expected if the GitHub Actions runner is x86_64; QEMU is set up to emulate ARM64 builds
- The workflow includes the `setup-qemu-action` to handle cross-platform builds

### Deploy Job Fails
- Verify SSH secrets are set correctly in GitHub
- Test SSH connection manually from your local machine:
  ```bash
  ssh -p <DEPLOY_PORT> <DEPLOY_USER>@<DEPLOY_HOST>
  ```
- Ensure `DEPLOY_PATH` exists and contains `docker-compose.yml` and `update.sh`
- Check server logs for any Docker errors

### Image Pull Fails on Server
- Ensure you've logged in to GHCR on the server:
  ```bash
  docker login ghcr.io
  ```
- Verify the image name in `server_deployment/docker-compose.yml` matches your GitHub username

## Additional Notes

- The memory limit of `512m` is enforced in the production `docker-compose.yml` to protect the 2GB RAM server.
- The `update.sh` script automatically cleans up old dangling images to save disk space.
- All timestamps in `update.sh` logs are in UTC (use `date -d "$(date -u)"` to convert locally if needed).
