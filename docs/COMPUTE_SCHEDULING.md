# SimpleAI Compute: scheduling, automatic placement, setup wizard

This document describes the proposed **draft** worker stack built on PRs #24-#29.
It is not production-ready until the release gates below pass.

## Architecture and privacy

- Proxy remains the OpenAI-compatible entry point.
- CPU workers dial **outbound HTTPS/WSS**, enroll with one-time tokens and persist
  their identity in an isolated volume. They require no incoming ports.
- A worker runs one `llama.cpp` model and one inference at a time.
- **All requests are private by default.** Unknown nodes cannot receive private
  prompts until an administrator explicitly trusts them. A remote owner can
  read any prompt processed on that computer even though transport uses TLS.
- Cloud/provider OAuth credentials never travel to workers. No personal account
  resale or marketplace is provided by this stack.

## 1. Worker telemetry

The worker sends cgroup CPU/RAM/disk limits and a busy flag. For every selected
GGUF it performs an actual CPU load and correctness test. It additionally
runs a bounded 64-token counting prompt and extracts `prompt_tps`,
`generation_tps` and load duration from **actual llama.cpp timing counters**,
when present. Unknown timings remain unknown rather than fake benchmarks.
Measurements are invalidated after the benchmark protocol/version changes.

The worker reports a maximum of one active slot. The proxy sanitizes all
numeric telemetry before displaying it; remote telemetry is untrusted.

## 2. Worker scheduler

For an **explicit model** (for example `local/qwen35-2b-q4`), the scheduler
only chooses between worker replicas of that *exact* model. It estimates:

`queue/load penalty + model load time + input tokens / prompt_tps + output tokens / generation_tps`

Input tokens are an approximate heuristic based on request size, **not**
an exact tokenizer or context-window proof. Workers with measured speed,
a warm model, and no active job are favored. Configured non-compute provider
positions are preserved.

A short bounded reservation is created **before** sending any job. The slot
is held until the final SSE chunk, normal response, error or cancellation.
When all nodes are busy, the v1 coordinator waits briefly (default 10 seconds,
configurable via `AIPROXY_COMPUTE_QUEUE_WAIT_SECONDS`, clamped to 0–60 seconds),
with at most 16 simultaneous waiters per node. It then falls back or fails
clearly; there is no durable queue yet. Run **one proxy server
process** for now: the reservation map and WebSocket connections are process-local.

Streaming cannot be transparently retried on another model once response
bytes have reached the caller.

## 3. Automatic model selection

`model: "local/auto"` is opt-in per API request. It chooses one approved,
actually supported model, and then schedules matching replicas. A caller may
set `aiproxy_compute_tier`:

| Tier | Preferred currently pinned models |
|------|----------------------------------|
| `fast` | up to Qwen 2B |
| `balanced` | Qwen 2B / 4B |
| `quality` | Qwen 4B / 9B |
| `auto` (default) | Heuristic from prompt size and output limit |

These are selection hints, **not** verified task-intelligence rankings.
An explicit tier does not silently downgrade if no model in its class
is available. `auto` may use the best available smaller model instead.
Explicit model identifiers never undergo substitution.

Example:

```bash
curl -sS https://YOUR_PROXY/v1/chat/completions \
  -H 'Authorization: Bearer YOUR_PROXY_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"model":"local/auto","aiproxy_compute_tier":"balanced","messages":[{"role":"user","content":"Summarize this note"}]}'
```

For intentionally public data sent to an untrusted node, a caller must
explicitly add `"aiproxy_visibility":"public"`. Do not mark private data public.

## 4. Model placement across machines

In `/admin/compute-nodes`, enable **Automatic per worker** (default is
**Manual** to protect existing installations). Global catalog approval is
still mandatory. On every new node heartbeat, the coordinator assigns up to
two models to the node, using Docker memory and model disk limits:

- Prefer the largest tested-fit model for more demanding jobs.
- Keep a smaller backup if the combined file size fits the disk budget.
- The actual worker runs a model before claiming it is supported.
- Only one model is active in RAM per CPU worker; switching has latency.

Initial estimates can be wrong, especially on small Raspberry Pi boards.
Measure real memory usage and benchmark on each device. Docker named-volume
disk caps are application-level, not kernel-enforced filesystem quotas.

## 5. Setup wizard

Open `/admin/compute-setup` as administrator:

1. Enter a reachable HTTPS proxy **origin**, a worker name, CPU/RAM/thread
   limits and disk download budget.
2. Generate and download `docker-compose.yml` and a separate `.env`.
   The one-time enrollment token expires in **15 minutes** and is returned
   with `Cache-Control: no-store`.
3. In the target **Linux AMD64 or ARM64** Docker host, keep both files in one
   private directory and run:

```bash
chmod 600 .env
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose logs --tail=50 compute-worker
```

4. Confirm **Online** in the proxy dashboard. After successful enrollment,
   remove the enrollment token from `.env` (worker identity persists in
   the Docker volume). Do not copy the volume to another physical computer.

The wizard does **not** run commands on the host. The GHCR worker image has
to be published and tested before this installation can work. The generated
Compose currently references the latest published image; pin a reviewed
version/digest for repeatable production use.

## 6. Acceptance / release gates

Before changing `main` or running on personal machines:

- [ ] PR #24 base worker CI and Docker AMD64 + ARM64 builds succeed
- [ ] Staging proxy + WSS works through external HTTPS/Cloudflare
- [ ] A real pinned GGUF is downloaded and its SHA256 verified on ARM
- [ ] Real install/load/inference/stream/unload/delete succeeds on ARM
- [ ] A second independent physical host enrolls, connects, and reconnects
- [ ] Friend-node private-prompt isolation and revocation are verified
- [ ] Unit/integration tests for telemetry, reservations and model placement pass
- [ ] One worker remains exclusive under concurrent requests
- [ ] CPU/RAM/disk usage stays within host limits under sustained inference
- [ ] Benchmark and selection improve measured response time over round-robin
- [ ] Setup wizard creates valid Compose, never echoes a token in logs
- [ ] Production deployment configuration and persistent DB are backed up
- [ ] A rollback to the prior proxy+worker version has been rehearsed

**Not in scope yet:** GPU acceleration, cross-device model sharding,
payment/billing, public P2P market, account sharing, persistent distributed
queues or multi-process coordinator replication.
