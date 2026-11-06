# SimpleAI Compute Worker

CPU-only GGUF worker for Linux Docker hosts, amd64 or arm64. Requires outbound
HTTPS/WSS, persistent volumes and sufficient local CPU, RAM and disk.

## Installation

1. Generate one-time enrollment token:
   POST /admin/api/compute/enrollments (admin Basic authentication).
2. Copy worker/docker-compose.yml and worker/.env.example to Docker host.
3. Rename .env.example to .env and enter the HTTPS URL and token.
4. Run docker compose up -d.

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

Unprivileged container with no Docker socket, no exposed ports, configurable
Docker CPU/memory limits and restricted model operations.
