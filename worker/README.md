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
   `docker-compose.yml`. **Only edit that one Compose file:** set
   `SIMPLEAI_PROXY_URL` to your external HTTPS proxy and paste a one-time token
   into `WORKER_ENROLLMENT_TOKEN`. Configure `mem_limit` / `memswap_limit`,
   `cpus`, `pids_limit`, `WORKER_THREADS`, and `WORKER_DISK_LIMIT_GB` there.
   Protect the Compose file (e.g. `chmod 600 docker-compose.yml`) since it
   initially contains the one-time enrollment token. The token is ignored once
   the worker identity is stored in the persistent worker-data volume.
4. Run `docker compose pull && docker compose up -d`. Check
   `docker compose logs --tail=100 compute-worker` and confirm the node
   appears as Online in the admin dashboard.
5. Worker defaults to `WORKER_REASONING=on`. The proxy controls reasoning per request: omit `think` (default on), set `"think":true` (on), or `"think":false` (off). The proxy converts this into llama.cpp `chat_template_kwargs.enable_thinking`, so the model does not reload. `reasoning_effort:"none"` also disables thinking. `WORKER_REASONING=auto` uses the model template default.\n6. Install then Load a small model (start with 0.8B on a low-RAM machine)
   from the dashboard. Verify a sample OpenAI-compatible request before
   granting access to private prompts.

Enrollment tokens expire after 15 minutes. Worker identity is stored in a
persistent data volume with permission 0600. Do not copy this volume to another
computer. Revoke nodes from the proxy administration.

## Automatic operation

After first enrollment, the administrator opens `/admin/compute-nodes` in
SimpleAI Proxy and checks the catalog models to make available. **Nothing on
the worker host needs to be configured for individual models.** The proxy
persists the selected list and delivers it over authenticated WSS, including
after reconnecting.

Each worker checks its CPU/RAM limit and disk quota first. Models that pass
preflight are downloaded from the pinned, SHA256-checked catalog, loaded,
given a real short inference test, and reported back as supported/error/
unsupported. The highest-priority supported model is warmed by default.
Other supported models can be selected by `local/<model-id>`; a worker
loads the requested model automatically when routed, without requiring
`install` or `load` from the admin. One model can be resident per CPU worker
at a time; switching models introduces startup latency.

`WORKER_DISK_LIMIT_GB` limits application-level bytes in `/models`. Old
cached GGUF files are evicted in least-recently-used order as required.
Each file is downloaded with a streaming byte cap, checked by SHA256 and
atomically published. The model cache can contain fewer models than have
passed tests; tested capabilities are persisted under `/data` and exposed
to SimpleAI Proxy. Changing the policy triggers tests for new models;
saving the unchanged list again re-runs tests.

**Storage limitation:** Compose has no portable hard disk quota for named
Docker volumes. The worker enforces a bounded *application* quota on model
downloads. This does not limit filesystem growth by other processes or provide
a kernel-enforced quota. Use a dedicated quota-managed filesystem if an
absolute OS-level cap is required. The cache and model downloads count toward
the configured cap; persistent identity/metadata lives separately in `/data`.

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

Thinking defaults to ON for compute requests, and the proxy can choose per
request: `"think": false` for a fast answer, `"think": true` for reasoning,
or omit the field to retain the default ON. No model reload is required.
The proxy automatically limits reasoning to one quarter of the specified
`max_tokens` (maximum 256), leaving room for a visible response. When the
caller omits `max_tokens`, the default reasoning budget is 192 tokens. The
caller can override with `"reasoning_budget_tokens": 128`; that value must
be a nonnegative integer smaller than the completion limit if specified.
For example, `{"think":true,"max_tokens":512}` allows up to 128 reasoning
tokens, leaving at least 384 generated tokens for an answer. Limits are
upper bounds, not guarantees of a correct or nonempty response.

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
