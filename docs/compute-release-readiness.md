# SimpleAI Compute — release readiness and execution order (2026-11-08)

Keep this checklist alongside the code so that continuing the project in another
conversation, PR or machine does not silently skip an acceptance gate.

## Current baseline

- Production main includes Compute Worker v1, the one-line Linux installer,
  Omarchy first-run fixes and corrected Auto Maximum host-RAM sizing.
- Existing user compute nodes may still have obsolete registration records;
  their Docker identities, model weights and local persistent volumes must be
  preserved unless an operator separately decommissions a node.
- Privacy is opt-in per node: friend's/untrusted compute hosts must not receive
  private prompts. Explicitly public requests can be routed to them.
- No API token, private user prompt, enrollment secret or machine credential
  belongs in a CI artifact, log or issue.

## PR release dependency order

1. **#47 — safe proxy release deployment.** Verify backup location/ownership
   on the actual VPS; staging Docker fault injection must pass. Merge only after
   an explicit release decision because main auto-deploys the proxy. Code adds
   native parallel AMD64/ARM64 builds, immutable digest manifest checks and
   snapshot + rollback gate.
2. **#45 — faster native Compute Worker builds.** PR smoke runners are already
   native amd64/arm64; main publishing path must be validated after merge
   without publishing an incomplete manifest.
3. **#46 — dynamic GGUF catalog and permanent registration removal.** Rebase
   after #45 because both edit `.github/workflows/compute-worker.yml`.
   Then validate a real pinned GGUF download and inference on isolated Oblak,
   plus a second independently enrolled amd64 worker.

All PRs must pass fresh CI after any rebase. Do not merge PRs merely because
previous branch CI was green; image releases and staging tests are separate.

## End-to-end checks (real hosts)

- [ ] On staging, fetch real Hugging Face LFS SHA256/commit and choose a second
      GGUF quantization not already bundled.
- [ ] Send catalog/policy from staging proxy across outbound WSS to ARM64 node,
      perform real self-test and SSE chat completion.
- [ ] Run `compute_staging_dynamic_smoke.py` inside isolated staging proxy;
      confirm its policy and catalog cleanup (weights may remain in LRU cache).
- [ ] Repeat model selection, on-demand switching, streaming and mixed-node
      `local/auto` routing on independently enrolled AMD64 node.
- [ ] Check disk cap/LRU eviction, interrupted downloads/restart and checksum
      mismatch handling. Cap is *application-level*, not a filesystem quota.
- [ ] Verify untrusted node never sees a private prompt; test explicitly
      public routing, revocation, permanent registration removal and
      absence of sensitive tokens in logs.
- [ ] Test reconnect after worker/proxy restart. Existing volume identity
      and cached model weights must survive a worker image upgrade.
- [ ] Enable host-local scheduled updater only on consenting, already
      registered hosts (PR #46 helper); no Docker socket mounted in worker.
- [ ] Confirm obsolete offline records removed only when known to be stale;
      remote containers are not deleted by proxy-side removal.
- [ ] Test database schema compatibility and the rollback path with a
      **production-like but isolated** SQLite copy, not just a synthetic DB.
- [ ] Verify current backup, previous-image digest, worker availability and
      `/v1/models` after production release.

## Exclusions / future work

- GPU, arbitrary executable Hugging Face repositories and non-GGUF backends
  are not supported by this CPU-only model protocol.
- Automatic host updates initiated from proxy UI are tracked in #39 and
  require per-host operator consent and limited authority. A central proxy
  never gets raw Docker socket or arbitrary shell execution on a friend's PC.
- Marketplace/payment/monetization work is a later milestone, not a reason
  to skip release security, privacy or worker reliability.
