# SimpleAI Compute Worker

CPU-only GGUF worker for Linux Docker hosts, amd64 or arm64. Requires outbound
HTTPS/WSS, persistent volumes and sufficient local CPU, RAM and disk.

## Installation

1. First deploy the proxy version containing PR #24 and publish the worker
   image. A draft PR alone does not make the image or endpoints available.
2. Open `/admin/compute-nodes` as an administrator and click **Generate
   15-minute enrollment token**. Alternatively POST
   `/admin/api/compute/enrollments` with admin Basic auth **and** the header
   `X-SimpleAI-Admin-Action: 1` (required to prevent cross-site writes).
3. On the Linux Docker host, copy `worker/docker-compose.yml` to
   `docker-compose.yml`, and `worker/.env.example` to `.env`.
   Set `SIMPLEAI_PROXY_URL` to the actual external HTTPS proxy origin and paste
   the one-time token into `WORKER_ENROLLMENT_TOKEN`. Keep `.env` private
   (for example, `chmod 600 .env`).
4. Run `docker compose pull && docker compose up -d`. Check
   `docker compose logs --tail=100 compute-worker` and confirm the node
   appears as Online in the admin dashboard.
5. Install then Load a small model (start with 0.8B on a low-RAM machine)
   from the dashboard. Verify a sample OpenAI-compatible request before
   granting access to private prompts.

Enrollment tokens expire after 15 minutes. Worker identity is stored in a
persistent data volume with permission 0600. Do not copy this volume to another
computer. Revoke nodes from the proxy administration.

## Models

Curated, checksum-verified GGUF entries are in compute_catalog.py.
Install, load, unload, delete through:
POST /admin/api/compute/nodes/{id}/operations
{"action":"install","model":"qwen35-4b-q4"}

Check:
GET /admin/api/compute/operations/{operation_id}

Ready endpoint:
POST /v1/chat/completions
{"model":"local/qwen35-4b-q4","messages":[{"role":"user","content":"Hello"}]}

Privacy: all inference is private by default. Only explicitly trusted
nodes (allow_private=true) may receive private prompts. To send an explicitly
public request to an untrusted node, set "aiproxy_visibility":"public".
The remote host operator can access prompt contents regardless of TLS.

Deleting a model also clears its per-model download cache to reclaim disk space.
Never copy `/data/identity.json` to another machine; generate a new enrollment
for each physical computer. If a node is revoked, its stored credentials cannot
be reused.

Unprivileged container with no Docker socket, no exposed ports, configurable
Docker CPU/memory limits and restricted model operations.

## Pre-production acceptance checks

- All proxy and worker GitHub CI tests pass, including both amd64 and arm64
  container builds.
- Public HTTPS endpoint and Cloudflare proxy accept outbound WSS upgrades;
  node stays connected and recovers after proxy/worker restart.
- Run a real SHA256-verified model download, load and chat-completion test on
  the target ARM VPS, then unload and delete it, checking released disk space.
- Repeat enrollment and inference from a separate Docker host.
- Confirm friend workers cannot receive private prompts by default. Test
  explicit Public requests, revocation, and disabling Private access.
- Keep the PR unmerged until those checks are complete, as merging into main
  triggers the existing production deployment workflow.
