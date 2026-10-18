# Registering icey-stats in vps-ops

Add these to `leonardovida/vps-ops` after the first successful deploy. Do not edit vps-ops from this repository.

## 1. `config/apps.tsv`

The `project` column is the Docker Compose project label. Dokploy uses the compose `appName`, which it usually gives a random suffix (for example `icey-stats-ab12cd`). Look it up after provisioning:

```sh
ssh root@178.105.139.229 "docker ps --filter label=com.docker.compose.service=icey-stats --format '{{.Label \"com.docker.compose.project\"}}'"
```

Then add this tab-separated row, with `<appName>` replaced by that value:

```tsv
icey-stats	<appName>	strict	-	~/workspace/personal/VPS/icey-stats
```

- `strict`: the container runs as uid 10001 with all capabilities dropped, `no-new-privileges` and a 256 MB memory limit, so `bin/audit` holds it to that.
- `secrets` is `-`. The only secret, the ingest token, is in the Dokploy compose env under `/etc/dokploy`, which the `platform` row already covers. The data is in the Docker volume `icey-stats-data` under `/var/lib/docker`, which the same row covers. `/var/lib/icey-stats/releases` holds only public source code.

## 2. `docs/server.md`, Workloads table

```markdown
| icey-stats | `~/workspace/personal/VPS/icey-stats` | Dokploy project `icey-stats` (Compose project `<appName>`), image built on the server from `git archive` by `scripts/deploy.py` over an SSH tunnel; public at `icey-stats.178-105-139-229.sslip.io`, Traefik rate limit 30/min per IP. Runs as uid 10001, read-only root, no capabilities, 256 MB / 0.5 CPU / 128 PIDs, no published ports | volume `icey-stats-data` (DuckDB and 14 daily backups), `/var/lib/icey-stats/releases` (3 kept) |
```

## 3. `docs/changelog.md`

Use the deploy date, and fill in the verification you actually ran:

```markdown
## YYYY-MM-DD

- Deployed icey-stats, anonymous usage statistics for the Icey macOS app (repository `leonardovida/icey-stats`). It is the new Dokploy project `icey-stats` with one raw Compose stack (Compose project `<appName>`), public at `https://icey-stats.178-105-139-229.sslip.io` through Traefik (Let's Encrypt, http to https redirect, rate limit 30/min with a burst of 60 per client IP, 1 MB request body cap). No other project, stack or network was changed.
  - Container: `strict` profile. uid 10001, read-only root with `tmpfs /tmp`, all capabilities dropped, `no-new-privileges`, 256 MB memory with no swap, 0.5 CPU, 128 PIDs, json-file logs 3 x 10 MB. No published ports. The admin API binds 127.0.0.1 inside the container only.
  - Data: DuckDB in the Docker volume `icey-stats-data`, with daily backups in the same volume (`/data/backups`, 14 kept). Release sources are in `/var/lib/icey-stats/releases` and images are `icey-stats:<commit>`; `scripts/deploy.py` keeps the newest three of each and never runs global prunes.
  - Secret: `ICEY_STATS_INGEST_TOKEN` is in the Dokploy compose env and the `leonardovida/icey` Actions secret.
  - Added `icey-stats` to `config/apps.tsv` with the `strict` profile.
  - Verified: `https://icey-stats.178-105-139-229.sslip.io/healthz` answers 200, `bin/health` is unchanged for the other apps, `bin/audit` passes the strict profile for `icey-stats`, and `bin/summary` in icey-stats returns data.
```
