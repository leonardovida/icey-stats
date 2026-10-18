# icey-stats

Anonymous usage statistics for [Icey](https://github.com/leonardovida/icey), the free macOS menu bar app. A small Python service writes them to DuckDB on the `ai-ybp` VPS, served at `https://icey-stats.178-105-139-229.sslip.io`.

It answers three questions without identifying anyone:

- How many installs are active, and on which macOS versions, Macs and app versions?
- Which features do people turn on?
- How many people visit the website and download a release?

## Privacy

This is the section to link from Icey's README.

**What Icey sends.** Once a day at most, Icey sends a small JSON report:

| Field | Example | Notes |
| --- | --- | --- |
| `schema`, `app_version`, `build` | `1`, `0.14.0`, `140` | report format and app version |
| `os_version` | `26.5.1` | macOS version |
| `model` | `Mac15,10` | hardware model identifier, the same for every Mac of that model |
| `cpu_arch`, `cpu_brand`, `cpu_cores` | `arm64`, `Apple M3 Max`, `14` | |
| `memory_gb`, `display_count`, `has_notch` | `36`, `2`, `true` | |
| `language` | `en`, `pt-BR` | language only |
| `features` | `{"launchAtLogin": true}` | up to 32 on/off flags for Icey features |
| `settings` | `{"spacing": "compact"}` | up to 16 short Icey setting values |

**What is never collected.** No identifier of any kind: no install ID, device ID, serial number, user name or host name. Icey sends no report ID, so one Mac's reports cannot be linked across days. Because each install sends at most one report a day, the number of reports on a day is roughly the number of active installs that day.

**What the server keeps.** The fields above, plus the UTC **day** and **hour** the report arrived. The service never stores or logs IP addresses or user agents, and never stores exact timestamps. Fields it does not know, and known fields with invalid values, are dropped before storage.

**Website counter.** The pages at `leonardovida.github.io/icey` count page views and download clicks. A count holds the day, the page path, the referring site's host name (for example `news.ycombinator.com`, never the full URL) and the event. It is a counter per day, not a row per visit. No cookies, no IP addresses, no user agents.

**GitHub numbers.** A daily GitHub Actions job in the Icey repository sends public repository numbers: stars, forks, release download counts and GitHub's 14-day traffic totals.

**Where it lives.** One DuckDB file on a server run by the maintainer, plus daily backups (the last 14) on the same server. It is not shared with anyone or sent to any third-party analytics service.

## Endpoints

Public, over HTTPS through Traefik. Traefik limits each client IP to an average of 30 requests a minute, with bursts of 60:

| Method and path | Body | Answer |
| --- | --- | --- |
| `POST /v1/report` | JSON report, `Content-Type: application/json`, 4 KB or less | `204`. `400` for invalid data, `413` too large, `415` wrong type, `429` when the daily safety cap (50,000 reports) is reached |
| `POST /v1/hit` | `{"path": "/icey/download/", "ref": "news.ycombinator.com", "event": "pageview" or "download"}`, as `text/plain` (what `navigator.sendBeacon` sends) or `application/json`, 1 KB or less | `204`. CORS allows only `https://leonardovida.github.io`, without credentials. Any other `Origin` gets `403` |
| `GET /v1/hit?path=...&ref=...&event=...` | | `204`, a fallback that works without JavaScript |
| `POST /v1/github` | JSON snapshot, 1 MB or less, `Authorization: Bearer $ICEY_STATS_INGEST_TOKEN` | `204`. `401` without a valid token, which is checked before the body is read |
| `GET /healthz` | | `200 ok` once the database answers |

Validation rules:

- Report: `schema` must be `1` and `app_version` must look like a version. Every other field is optional. A known field with the wrong type or an out-of-range value is dropped, and so is each invalid entry in `features` and `settings`, so one bad field never loses the report.
- Hit: `path` must match `^/icey(/[a-zA-Z0-9._-]*)*/?$` (120 characters at most), `ref` must be empty or a bare host name, and `event` defaults to `pageview`.
- Snapshot: it must be for `leonardovida/icey`. The GitHub Actions job that sends it is in [docs/examples/icey-usage-stats.yml](docs/examples/icey-usage-stats.yml).

Website snippet:

```html
<script>
  addEventListener("load", () => {
    const ref = document.referrer ? new URL(document.referrer).hostname : "";
    const send = (event) => navigator.sendBeacon("https://icey-stats.178-105-139-229.sslip.io/v1/hit",
      JSON.stringify({ path: location.pathname, ref, event }));
    send("pageview");
    document.querySelectorAll("a[href$='.dmg'], a[href*='/releases/']").forEach((a) =>
      a.addEventListener("click", () => send("download")));
  });
</script>
```

## Data model

| Table | Key | Contents |
| --- | --- | --- |
| `reports` | none, one row per report | `received_day`, `received_hour` (0-23 UTC), `report_schema`, the report fields, `features MAP(VARCHAR, BOOLEAN)`, `settings MAP(VARCHAR, VARCHAR)` |
| `page_hits` | `day, path, ref, event` | `count`, increased by an upsert. After 2,000 rows in a day, new combinations count under `(other)` |
| `github_snapshots` | none | `collected_at`, `payload JSON`, each snapshot as sent |
| `release_downloads` | `collected_day, tag, asset` | `download_count`. The day's last snapshot wins |
| `traffic_daily` | `day, kind` (`views` or `clones`) | `count`, `uniques`. Upserted per day from GitHub's rolling 14 days |
| `referrers_daily`, `paths_daily` | `collected_day, referrer` and `collected_day, path` | GitHub's 14-day top referrers and paths as of that day. The last snapshot of the day wins |
| `repo_daily` | `collected_day` | `stars`, `forks`, `watchers`, `open_issues` |
| `schema_version` | | migrations applied; startup runs the missing ones in order |

A snapshot section that is missing (for example `traffic` when the job has no token for it) leaves the stored rows unchanged.

## Querying

There is no admin token. The admin API listens only on `127.0.0.1:8081` inside the container, which Traefik cannot route to and no host port publishes. The scripts below reach it over SSH with `docker exec`:

```sh
bin/summary            # JSON for the last 30 days: reports per day, OS versions, models, CPUs,
bin/summary 7          # app versions, feature adoption, page hits, downloads per release, traffic
bin/query "SELECT os_version, count(*) AS reports FROM reports
           WHERE received_day > current_date - 7 GROUP BY ALL ORDER BY reports DESC"
bin/query --json "SUMMARIZE reports"
bin/query < report.sql
```

`bin/query` accepts exactly one `SELECT`, `WITH`, `DESCRIBE`, `SUMMARIZE` or `SHOW` statement. Several layers enforce this:

- a leading-keyword check, plus DuckDB's own parser classifying the statement;
- a `READ ONLY` transaction;
- a 10-second timeout and a 1,000-row cap;
- one admin query at a time.

DuckDB runs with `enable_external_access=false` and a locked configuration, so no SQL can read or write files (`read_csv`, `COPY`, `ATTACH`, `INSTALL` all fail). Inside the container itself: `python -m icey_stats.admin summary|query|backup`.

Useful queries:

```sql
-- daily active installs (one report per install per day)
SELECT received_day, count(*) FROM reports GROUP BY ALL ORDER BY 1;
-- adoption of one feature in the last 30 days
SELECT avg(features['launchAtLogin']::INT) FROM reports WHERE received_day > current_date - 30;
-- downloads per release, latest snapshot
SELECT tag, sum(download_count) FROM release_downloads
WHERE collected_day = (SELECT max(collected_day) FROM release_downloads) GROUP BY ALL ORDER BY 2 DESC;
```

## Development

```sh
uv sync
uv run pytest
ICEY_STATS_INGEST_TOKEN=$(openssl rand -hex 32) ICEY_STATS_DB_PATH=./data/dev.duckdb uv run python -m icey_stats
```

Dependencies are pinned in `uv.lock`. The image installs from `requirements.lock`, a hashed export of it. After changing dependencies, run `uv export --frozen --no-dev --no-emit-project -o requirements.lock`; a test fails when the two drift.

## Deploy

This is the same pattern as torre-dei-beati. The image is built on the server from `git archive` of a commit pushed to `origin/main`, tagged `icey-stats:<commit>`, labelled with `org.opencontainers.image.revision`, and run by a dedicated Dokploy raw Compose stack ([deploy/compose.yml](deploy/compose.yml)). The scripts read the Dokploy API key and SSH target from the 1Password Personal Apps mount (`$HOME/.config/personal-apps/.env`, keys `VPS_ADMIN__DOKPLOY_AUTH_TOKEN` and `VPS_ADMIN__SSH_SERVER`) and never print them. They reach Dokploy through a short-lived SSH tunnel to `127.0.0.1:3000`.

1. Once: `uv run scripts/provision.py`. This creates the Dokploy project `icey-stats` with one raw Compose stack and prints its `composeId`. It refuses to run if a project or compose with that name already exists, or if `deploy/target.json` already has a `composeId`. Put the ID in `deploy/target.json`, then commit and push.
2. Every release: `uv run scripts/deploy.py deploy`. It needs a clean tree with HEAD on `origin/main`, and it:
   - runs the tests;
   - checks the server has 1 GiB of free memory, 3 GiB of free disk and load below the CPU count;
   - builds the image on the server and verifies its label;
   - keeps the ingest token already in the compose env, or generates one on the first deploy;
   - updates the env and compose file, deploys, and waits for the status and a healthy container (non-root, read-only, no capabilities, no published ports);
   - polls `https://<host>/healthz` until it answers 200;
   - sets the `ICEY_STATS_INGEST_TOKEN` Actions secret on `leonardovida/icey`, piping the token to `gh` on stdin;
   - prunes this app's release directories and images, keeping the newest three.
3. Roll back: `uv run scripts/deploy.py rollback <commit>` redeploys a retained image, using the compose file from that commit. A rollback across a schema migration fails safely: the older release refuses to start on a newer schema and the container never becomes healthy. Restore a backup in that case.
4. `uv run scripts/deploy.py prune` runs the pruning step alone.

## Backups and restore

Once a day, and again on every shutdown (SIGTERM), the service writes `/data/backups/icey-stats-YYYYMMDD.duckdb`. It runs `CHECKPOINT` and copies the file in Python while it holds the write lock, so the copy is consistent and DuckDB's external access stays off. It keeps the last 14. On the host they are in `/var/lib/docker/volumes/icey-stats-data/_data/backups/`. `bin/query` cannot read them, by design.

To restore:

1. Run `python -m icey_stats.admin backup` in the container, so the current state is saved first.
2. Stop the stack in Dokploy.
3. Copy the chosen backup over `/var/lib/docker/volumes/icey-stats-data/_data/icey-stats.duckdb` and keep the owner `10001:10001`.
4. Remove any `icey-stats.duckdb.wal` next to it.
5. Start the stack.

## Runtime limits

- One process, one uvicorn worker per listener, one DuckDB connection, with writes serialized.
- Container: uid 10001, read-only root, `tmpfs /tmp`, no capabilities, `no-new-privileges`, 256 MB memory with no swap, 0.5 CPU, 128 PIDs, json-file logs (3 x 10 MB).
- DuckDB: 96 MB memory limit, 1 thread.
- Logs are JSON lines that record only failed or slow requests (method, path, status, duration), plus startup, backup and shutdown events. They never contain addresses, user agents or payloads.
