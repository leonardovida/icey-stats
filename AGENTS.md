# Agent rules for icey-stats

This service runs on the shared production VPS `ai-ybp`, next to other people's apps. It collects anonymous statistics, and its README promises users what is and is not stored. Keep that promise.

## Privacy

- Never store, log or derive client IP addresses, user agents, cookies, exact timestamps, or any install, device or user identifier. Reports keep only the UTC day and hour.
- Validate every field strictly and drop unknown fields. A new field needs validation, a migration, tests and a README privacy table update in the same change.
- Never add third-party analytics, tracking pixels or outbound calls from the service.
- Request logs record method, path, status and duration only, and only for failed or slow requests. Do not log bodies, headers or query strings.

## Runtime

- Keep the `strict` profile of vps-ops `config/apps.tsv`: non-root uid 10001, `cap_drop: ALL`, `no-new-privileges`, read-only root with `tmpfs /tmp`, a memory limit (256m), `cpus: 0.5`, `pids_limit: 128`, rotated json-file logs, a healthcheck. Never publish a host port, mount the Docker socket, or join any network other than `dokploy-network`.
- One process owns the DuckDB file; writes go through the `Database` lock. Never open the production file from a second process while the service runs.
- Keep `enable_external_access=false` and `lock_configuration=true`. Solve anything that seems to need file access in Python. Backups, for example, are copied in Python.
- The admin listener binds `127.0.0.1` inside the container and is never routed by Traefik. Do not add an admin token or a public admin route.
- Migrations only append. Never edit a migration that has been released.

## Secrets

- The only secret is `ICEY_STATS_INGEST_TOKEN`. It lives in the Dokploy compose env and in the `leonardovida/icey` Actions secret. `scripts/deploy.py` generates it once and keeps it. Never print, log or commit it, and never pass it as a command-line argument.
- Release scripts read the Dokploy key from the 1Password mount `$HOME/.config/personal-apps/.env`. It is a FIFO: read it once and never print it. Tests must never read it.

## Changes

- Run `uv run pytest` before committing. For image changes, also `docker build` and run a local smoke test.
- Deploy only with `scripts/deploy.py` from a clean tree on `origin/main`. Never deploy from a dirty tree, never run global Docker prunes, and never touch another app's project, stack, volume or image.
- Server-wide changes, and the app's row in `config/apps.tsv`, go through vps-ops and its changelog (see [docs/vps-ops-registration.md](docs/vps-ops-registration.md)).
