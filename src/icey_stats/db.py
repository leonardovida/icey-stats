"""The DuckDB database: one connection owned by the process, writes serialized by a lock.

DuckDB allows a single writer process. This service is that process: it opens the file once at
startup with external access disabled and the configuration locked, so no SQL it runs (including the
admin read-only queries) can read or write any other file in the container.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb

log = logging.getLogger("icey_stats.db")

# Each migration runs once, in order, inside a transaction. Never edit a released migration; append.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """CREATE TABLE reports (
                received_day   DATE     NOT NULL,
                received_hour  UTINYINT NOT NULL,
                report_schema  USMALLINT NOT NULL,
                app_version    VARCHAR  NOT NULL,
                build          INTEGER,
                os_version     VARCHAR,
                model          VARCHAR,
                cpu_arch       VARCHAR,
                cpu_brand      VARCHAR,
                cpu_cores      USMALLINT,
                memory_gb      USMALLINT,
                display_count  UTINYINT,
                has_notch      BOOLEAN,
                language       VARCHAR,
                features       MAP(VARCHAR, BOOLEAN),
                settings       MAP(VARCHAR, VARCHAR)
            )""",
            """CREATE TABLE page_hits (
                day    DATE    NOT NULL,
                path   VARCHAR NOT NULL,
                ref    VARCHAR NOT NULL,
                event  VARCHAR NOT NULL,
                count  BIGINT  NOT NULL,
                PRIMARY KEY (day, path, ref, event)
            )""",
            """CREATE TABLE github_snapshots (
                collected_at TIMESTAMP NOT NULL,
                payload      JSON      NOT NULL
            )""",
            """CREATE TABLE release_downloads (
                collected_day  DATE    NOT NULL,
                tag            VARCHAR NOT NULL,
                asset          VARCHAR NOT NULL,
                download_count BIGINT  NOT NULL,
                PRIMARY KEY (collected_day, tag, asset)
            )""",
            """CREATE TABLE traffic_daily (
                day     DATE    NOT NULL,
                kind    VARCHAR NOT NULL,
                count   BIGINT  NOT NULL,
                uniques BIGINT  NOT NULL,
                PRIMARY KEY (day, kind)
            )""",
            """CREATE TABLE referrers_daily (
                collected_day DATE    NOT NULL,
                referrer      VARCHAR NOT NULL,
                count         BIGINT  NOT NULL,
                uniques       BIGINT  NOT NULL,
                PRIMARY KEY (collected_day, referrer)
            )""",
            """CREATE TABLE paths_daily (
                collected_day DATE    NOT NULL,
                path          VARCHAR NOT NULL,
                title         VARCHAR NOT NULL,
                count         BIGINT  NOT NULL,
                uniques       BIGINT  NOT NULL,
                PRIMARY KEY (collected_day, path)
            )""",
            """CREATE TABLE repo_daily (
                collected_day DATE   NOT NULL PRIMARY KEY,
                stars         BIGINT NOT NULL,
                forks         BIGINT NOT NULL,
                watchers      BIGINT NOT NULL,
                open_issues   BIGINT NOT NULL
            )""",
        ],
    ),
]
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]

OVERFLOW = "(other)"
BACKUP_NAME = re.compile(r"icey-stats-(\d{8})\.duckdb")
READONLY_KEYWORDS = frozenset({"SELECT", "WITH", "DESCRIBE", "SUMMARIZE", "SHOW"})
MAX_QUERY_LENGTH = 20_000


class CapacityError(Exception):
    """The daily safety cap was reached; the request is dropped."""


class QueryRejected(ValueError):
    """The admin SQL is not a single read-only statement."""


class SchemaTooNew(RuntimeError):
    """The database was migrated by a newer release; refuse to run an older one against it."""


def utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    def __init__(
        self,
        path: str,
        *,
        backup_dir: str | None = None,
        backup_keep: int = 14,
        memory_limit: str = "96MB",
        threads: int = 1,
        max_reports_per_day: int = 50_000,
        max_hit_rows_per_day: int = 2_000,
        query_timeout_seconds: float = 10.0,
        query_row_cap: int = 1000,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.path = path
        self.backup_dir = backup_dir or os.path.join(os.path.dirname(os.path.abspath(path)), "backups")
        self.backup_keep = backup_keep
        self.max_reports_per_day = max_reports_per_day
        self.max_hit_rows_per_day = max_hit_rows_per_day
        self.query_timeout_seconds = query_timeout_seconds
        self.query_row_cap = query_row_cap
        self._clock = clock
        self._lock = threading.RLock()
        self._query_slot = threading.Semaphore(1)
        self._con = duckdb.connect(
            path,
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
                "allow_community_extensions": False,
                "allow_unsigned_extensions": False,
                "memory_limit": memory_limit,
                "threads": threads,
            },
        )
        self._con.execute("SET lock_configuration = true")
        self.migrate()

    # ------------------------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def schema_version(self) -> int:
        with self._lock:
            self._con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = self._con.execute("SELECT max(version) FROM schema_version").fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def migrate(self) -> int:
        with self._lock:
            current = self.schema_version()
            if current > LATEST_SCHEMA_VERSION:
                raise SchemaTooNew(f"database schema {current} is newer than this release ({LATEST_SCHEMA_VERSION})")
            for version, statements in MIGRATIONS:
                if version <= current:
                    continue
                self._con.execute("BEGIN TRANSACTION")
                try:
                    for statement in statements:
                        self._con.execute(statement)
                    self._con.execute("INSERT INTO schema_version VALUES (?)", [version])
                    self._con.execute("COMMIT")
                except Exception:
                    self._con.execute("ROLLBACK")
                    raise
                log.info("schema migrated", extra={"fields": {"version": version}})
                current = version
            return current

    def healthy(self) -> bool:
        with self._lock:
            row = self._con.execute("SELECT 1").fetchone()
        return row == (1,)

    # ---------------------------------------------------------------------------------------- writes

    def insert_report(self, report: dict[str, Any]) -> None:
        now = self._clock().astimezone(UTC)
        day = now.date()
        with self._lock:
            (count,) = self._con.execute("SELECT count(*) FROM reports WHERE received_day = ?", [day]).fetchone()
            if count >= self.max_reports_per_day:
                raise CapacityError("daily report cap reached")
            self._con.execute(
                """INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?::MAP(VARCHAR, BOOLEAN), ?::MAP(VARCHAR, VARCHAR))""",
                [
                    day,
                    now.hour,
                    report["schema"],
                    report["app_version"],
                    report["build"],
                    report["os_version"],
                    report["model"],
                    report["cpu_arch"],
                    report["cpu_brand"],
                    report["cpu_cores"],
                    report["memory_gb"],
                    report["display_count"],
                    report["has_notch"],
                    report["language"],
                    report["features"],
                    report["settings"],
                ],
            )

    def record_hit(self, hit: dict[str, str]) -> None:
        day = self._clock().astimezone(UTC).date()
        path, ref, event = hit["path"], hit["ref"], hit["event"]
        with self._lock:
            exists = self._con.execute(
                "SELECT 1 FROM page_hits WHERE day = ? AND path = ? AND ref = ? AND event = ?", [day, path, ref, event]
            ).fetchone()
            if not exists:
                (rows,) = self._con.execute("SELECT count(*) FROM page_hits WHERE day = ?", [day]).fetchone()
                if rows >= self.max_hit_rows_per_day:
                    # Bound the table size under abuse: new combinations fold into one bucket per event.
                    path, ref = OVERFLOW, OVERFLOW
            self._con.execute(
                """INSERT INTO page_hits VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT (day, path, ref, event) DO UPDATE SET count = count + 1""",
                [day, path, ref, event],
            )

    def ingest_github(self, snapshot: dict[str, Any], payload: Any) -> None:
        day = snapshot["collected_day"]
        con = self._con
        with self._lock:
            con.execute("BEGIN TRANSACTION")
            try:
                con.execute(
                    "INSERT INTO github_snapshots VALUES (?, ?::JSON)",
                    [snapshot["collected_at"], json.dumps(payload, separators=(",", ":"))],
                )
                repo = snapshot["repo"]
                con.execute(
                    """INSERT INTO repo_daily VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT (collected_day) DO UPDATE SET stars = excluded.stars, forks = excluded.forks,
                           watchers = excluded.watchers, open_issues = excluded.open_issues""",
                    [day, repo["stars"], repo["forks"], repo["watchers"], repo["open_issues"]],
                )
                # Per-day snapshots: the latest ingest of the day replaces the earlier one.
                if snapshot["downloads"] is not None:
                    con.execute("DELETE FROM release_downloads WHERE collected_day = ?", [day])
                    if snapshot["downloads"]:
                        con.executemany(
                            "INSERT INTO release_downloads VALUES (?, ?, ?, ?)",
                            [[day, tag, asset, n] for (tag, asset), n in snapshot["downloads"].items()],
                        )
                if snapshot["referrers"] is not None:
                    con.execute("DELETE FROM referrers_daily WHERE collected_day = ?", [day])
                    if snapshot["referrers"]:
                        con.executemany(
                            "INSERT INTO referrers_daily VALUES (?, ?, ?, ?)",
                            [[day, ref, c, u] for ref, (c, u) in snapshot["referrers"].items()],
                        )
                if snapshot["paths"] is not None:
                    con.execute("DELETE FROM paths_daily WHERE collected_day = ?", [day])
                    if snapshot["paths"]:
                        con.executemany(
                            "INSERT INTO paths_daily VALUES (?, ?, ?, ?, ?)",
                            [[day, p, title, c, u] for p, (title, c, u) in snapshot["paths"].items()],
                        )
                # GitHub reports a rolling 14-day window, so each day is upserted, never deleted.
                if snapshot["traffic"]:
                    con.executemany(
                        """INSERT INTO traffic_daily VALUES (?, ?, ?, ?)
                           ON CONFLICT (day, kind) DO UPDATE SET count = excluded.count, uniques = excluded.uniques""",
                        [[d, kind, c, u] for (d, kind), (c, u) in snapshot["traffic"].items()],
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    # ----------------------------------------------------------------------------------------- reads

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        # A cursor is a separate connection to the same database, safe to use from another thread.
        with self._lock:
            return self._con.cursor()

    def _read(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        cur = self._cursor()
        try:
            cur.execute("BEGIN TRANSACTION READ ONLY")
            cur.execute(sql, params or [])
            columns = [d[0] for d in cur.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
            cur.execute("ROLLBACK")
            return rows
        finally:
            cur.close()

    def summary(self, days: int = 30) -> dict[str, Any]:
        days = max(1, min(days, 366))
        today = self._clock().astimezone(UTC).date()
        since = today - timedelta(days=days - 1)

        def top(column: str, limit: int = 15) -> list[dict[str, Any]]:
            return self._read(
                f"""SELECT coalesce(CAST({column} AS VARCHAR), '(unknown)') AS value, count(*) AS reports
                    FROM reports WHERE received_day >= ? GROUP BY ALL ORDER BY reports DESC, value LIMIT {limit}""",
                [since],
            )

        latest_downloads = self._read(
            """SELECT tag, sum(download_count) AS downloads, max(collected_day) AS collected_day
               FROM release_downloads WHERE collected_day = (SELECT max(collected_day) FROM release_downloads)
               GROUP BY tag ORDER BY downloads DESC, tag"""
        )
        return {
            "window": {"since": since, "until": today, "days": days},
            "reports_per_day": self._read(
                """SELECT received_day AS day, count(*) AS reports FROM reports
                   WHERE received_day >= ? GROUP BY ALL ORDER BY day""",
                [since],
            ),
            "app_versions": top("app_version"),
            "os_versions": top("os_version"),
            "models": top("model"),
            "cpu_brands": top("cpu_brand"),
            "cpu_arches": top("cpu_arch"),
            "memory_gb": top("memory_gb"),
            "languages": top("language"),
            "feature_adoption": self._read(
                """SELECT e.key AS feature, count(*) AS reports,
                          round(100.0 * avg(CASE WHEN e.value THEN 1 ELSE 0 END), 1) AS pct_enabled
                   FROM (SELECT unnest(map_entries(features)) AS e FROM reports
                         WHERE received_day >= ? AND features IS NOT NULL)
                   GROUP BY ALL ORDER BY feature""",
                [since],
            ),
            "page_hits_per_day": self._read(
                """SELECT day, event, sum(count) AS hits FROM page_hits WHERE day >= ?
                   GROUP BY ALL ORDER BY day, event""",
                [since],
            ),
            "top_paths": self._read(
                """SELECT path, event, sum(count) AS hits FROM page_hits WHERE day >= ?
                   GROUP BY ALL ORDER BY hits DESC, path LIMIT 20""",
                [since],
            ),
            "top_referrers": self._read(
                """SELECT ref, sum(count) AS hits FROM page_hits WHERE day >= ? AND ref <> ''
                   GROUP BY ALL ORDER BY hits DESC, ref LIMIT 20""",
                [since],
            ),
            "downloads_per_release": latest_downloads,
            "repo": (self._read("SELECT * FROM repo_daily ORDER BY collected_day DESC LIMIT 1") or [None])[0],
            "github_traffic": self._read(
                "SELECT day, kind, count, uniques FROM traffic_daily WHERE day >= ? ORDER BY day, kind", [since]
            ),
        }

    def run_readonly_query(self, sql: str) -> dict[str, Any]:
        if not self._query_slot.acquire(timeout=self.query_timeout_seconds):
            raise QueryRejected("another admin query is running")
        try:
            cur = self._cursor()
        except BaseException:
            self._query_slot.release()
            raise
        try:
            check_readonly_sql(cur, sql)
        except BaseException:
            cur.close()
            self._query_slot.release()
            raise
        timer = threading.Timer(self.query_timeout_seconds, cur.interrupt)
        try:
            cur.execute("BEGIN TRANSACTION READ ONLY")
            timer.start()
            try:
                cur.execute(sql)
                columns = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchmany(self.query_row_cap + 1)
            except duckdb.InterruptException as exc:
                raise QueryRejected(f"query exceeded {self.query_timeout_seconds:g}s and was cancelled") from exc
            except duckdb.Error as exc:
                raise QueryRejected(str(exc).splitlines()[0][:300]) from exc
            finally:
                timer.cancel()
            truncated = len(rows) > self.query_row_cap
            return {"columns": columns, "rows": [list(r) for r in rows[: self.query_row_cap]], "truncated": truncated}
        finally:
            try:
                cur.execute("ROLLBACK")
            except duckdb.Error:
                pass
            cur.close()
            self._query_slot.release()

    # --------------------------------------------------------------------------------------- backups

    def backup_path(self, day: date) -> str:
        return os.path.join(self.backup_dir, f"icey-stats-{day:%Y%m%d}.duckdb")

    def backup_exists(self, day: date | None = None) -> bool:
        return os.path.exists(self.backup_path(day or self._clock().astimezone(UTC).date()))

    def backup(self, day: date | None = None) -> str:
        """CHECKPOINT and copy the database file while holding the write lock, then rotate.

        The copy is done by Python, not by DuckDB, so external access stays disabled.
        """
        day = day or self._clock().astimezone(UTC).date()
        os.makedirs(self.backup_dir, mode=0o750, exist_ok=True)
        dest = self.backup_path(day)
        with self._lock:
            for attempt in range(10):
                try:
                    self._con.execute("CHECKPOINT")
                    break
                except duckdb.Error:
                    if attempt == 9:
                        raise
                    time.sleep(0.5)
            _copy_atomically(self.path, dest)
            wal = self.path + ".wal"
            if os.path.exists(wal) and os.path.getsize(wal) > 0:
                _copy_atomically(wal, dest + ".wal")
            elif os.path.exists(dest + ".wal"):
                os.remove(dest + ".wal")
        removed = rotate_backups(self.backup_dir, self.backup_keep)
        log.info("backup written", extra={"fields": {"file": os.path.basename(dest), "removed": len(removed)}})
        return dest


def _copy_atomically(src: str, dest: str) -> None:
    partial = dest + ".partial"
    with open(src, "rb") as reader, open(partial, "wb") as writer:
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    os.replace(partial, dest)


def rotate_backups(backup_dir: str, keep: int) -> list[str]:
    """Keep the newest `keep` daily backups; remove older ones and stale partial copies."""
    try:
        names = os.listdir(backup_dir)
    except FileNotFoundError:
        return []
    dated = sorted((n for n in names if BACKUP_NAME.fullmatch(n)), reverse=True)
    removed: list[str] = []
    for name in dated[keep:]:
        for candidate in (name, name + ".wal"):
            path = os.path.join(backup_dir, candidate)
            if os.path.exists(path):
                os.remove(path)
                removed.append(candidate)
    for name in names:
        if name.endswith(".partial") and name.startswith("icey-stats-"):
            os.remove(os.path.join(backup_dir, name))
            removed.append(name)
    return removed


_LEADING_NOISE = re.compile(r"\A(?:\s+|--[^\n]*(?:\n|\Z)|/\*.*?\*/|\()*", re.DOTALL)


def check_readonly_sql(con: duckdb.DuckDBPyConnection, sql: Any) -> None:
    """Accept exactly one SELECT/WITH/DESCRIBE/SUMMARIZE/SHOW statement, or raise QueryRejected.

    Two independent checks: the leading keyword, and DuckDB's own parser classifying the single
    statement as a SELECT. The query then also runs inside a READ ONLY transaction.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise QueryRejected("query is empty")
    if len(sql) > MAX_QUERY_LENGTH:
        raise QueryRejected("query is too long")
    rest = _LEADING_NOISE.sub("", sql, count=1)
    keyword = re.match(r"[A-Za-z]+", rest)
    if not keyword or keyword.group(0).upper() not in READONLY_KEYWORDS:
        raise QueryRejected("only a single SELECT, WITH, DESCRIBE, SUMMARIZE or SHOW statement is allowed")
    try:
        statements = con.extract_statements(sql)
    except duckdb.Error as exc:
        raise QueryRejected(str(exc).splitlines()[0][:300]) from exc
    if len(statements) != 1:
        raise QueryRejected("exactly one statement is allowed")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise QueryRejected("only read-only statements are allowed")
