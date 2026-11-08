# Safe SimpleAI production rollout / rollback

This runbook corresponds to `server_deployment/update.sh` and GitHub issue #40.
Do not merge the release branch until the staging failure-injection test has
passed with real Docker and a production-like database copy.

## Before each deployment

- The running `llm-proxy` must exist; Docker must expose its exact local
  `sha256:...` image ID. An unknown previous image fails closed.
- `python3`, Docker Compose, and `flock` must be available on the VPS.
- `.env`, `config.yml`, `app.db`, and persistent `cache/` remain local.
- The SSH deployment user needs read/write access to the database and the
  private local snapshot folder.
- `server_deployment/docker-compose.yml`, `update.sh`,
  `backup_release.py`, and `check_release.py` are copied into the SAME
  production directory by deploy.yml's `strip_components: 1`.

## Automatic rollout

1. `flock` prevents simultaneous updates.
2. The SQLite `backup()` API creates a consistent snapshot (including
   committed WAL transactions) to
   `.release-backups/YYYYMMDDTHHMMSSZ-random/`; both original and backup
   must pass `PRAGMA integrity_check`. Private config and env are captured
   with restrictive permissions. An invalid backup aborts the release.
3. Record and tag the existing running image by immutable SHA256 image ID.
   Do not use mutable `:latest` to designate the rollback target.
4. Pull the candidate and recreate ONLY `llm-proxy`, preserving volumes,
   `config.yml`, `app.db`, and the external `proxy` network.
5. Check HTTP health, Basic-auth admin UI, compute node and model policy
   schemas, and authenticated `/v1/models` against the running container.
6. On failure *after replacement*, recreate from the locally retained old
   image and verify root HTTP health. Never overwrite a newer live database
   automatically: newer schema/data may not be compatible with old code.
7. On success, retain both rollback image and backup. No automatic prune.

**A rollback cannot guarantee full recovery from an irreversible DB schema
migration.** Investigate the log and use the manual restoration path below if
the previous image cannot read the upgraded DB.

## Manual DB recovery (last resort only)

Only if verified image rollback cannot recover the service, and after
reviewing what new data would be lost:

1. Stop every process that might write the DB, beginning with
   `docker compose stop llm-proxy`. Do not run restore while any writer
   remains connected.
2. Pick a snapshot with a valid `metadata.json` and verify its
   `app.db` using Python sqlite3 `PRAGMA integrity_check`.
3. Create a new, private `.release-recovery/<timestamp>/` and MOVE the
   current `app.db`, `app.db-wal`, and `app.db-shm` files (if present)
   into it, preserving post-failure data. Do not delete them.
4. Copy `SNAPSHOT/app.db` into `./app.db` with correct ownership/permissions
   and ensure the stale WAL/SHM sidecars are not present in the live path.
   Only restore `.env` / `config.yml` if specifically needed.
5. Restart the previous **immutable tagged image** (not the candidate
   `:latest`), or restart the already-created rollback container. Validate
   health, admin, worker connectivity and models before accepting traffic.

Do not execute `docker compose up -d` against the mutable latest image
during manual rollback without an image override. Never run
`docker compose down -v`, `docker volume prune`, or remove model/identity
volumes.

## Acceptance gates before production merge

- Run `pytest -q tests/test_safe_production_release.py`: WAL-consistent
  backup, corruption fail-closed and simulated failed post-deploy rollback.
- Run one **actual Docker staging** deployment and deliberately make the
  candidate fail a post-start check, then verify the previous immutable image
  is running and the backup/worker data survives.
- Validate a real ARM64 Compute Worker inference and a separate AMD64
  node after a rollback or restart.
- Require the native parallel multiarch release workflow (PR #45) to
  assemble and verify both manifests. CI success cannot prove live SSH
  deployment or recovery; those gates remain manual and explicit.
