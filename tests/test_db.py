from __future__ import annotations

import os
from datetime import date, timedelta

import duckdb
import pytest

from icey_stats.db import (
    LATEST_SCHEMA_VERSION,
    CapacityError,
    Database,
    QueryRejected,
    SchemaTooNew,
    rotate_backups,
)
from icey_stats.validation import validate_github, validate_report

from .conftest import NOW, Clock, full_report, github_snapshot


def snapshot(**overrides: object) -> tuple[dict, dict]:
    data = github_snapshot(**overrides)
    return validate_github(data, expected_repository="leonardovida/icey", now=NOW + timedelta(days=2)), data


# ----------------------------------------------------------------------------------------- schema


def test_migrations_are_idempotent(db_path: str, clock: Clock) -> None:
    first = Database(db_path, clock=clock)
    assert first.schema_version() == LATEST_SCHEMA_VERSION
    first.insert_report(validate_report(full_report()))
    assert first.migrate() == LATEST_SCHEMA_VERSION
    first.close()
    second = Database(db_path, clock=clock)
    assert second.schema_version() == LATEST_SCHEMA_VERSION
    assert second._read("SELECT count(*) AS n FROM schema_version")[0]["n"] == LATEST_SCHEMA_VERSION
    assert second._read("SELECT count(*) AS n FROM reports")[0]["n"] == 1
    second.close()


def test_newer_schema_is_refused(db_path: str, clock: Clock) -> None:
    Database(db_path, clock=clock).close()
    con = duckdb.connect(db_path)
    con.execute("INSERT INTO schema_version VALUES (?)", [LATEST_SCHEMA_VERSION + 1])
    con.close()
    with pytest.raises(SchemaTooNew):
        Database(db_path, clock=clock)


# ---------------------------------------------------------------------------------------- reports


def test_report_stores_day_and_hour_only(db: Database) -> None:
    db.insert_report(validate_report(full_report()))
    row = db._read("SELECT * FROM reports")[0]
    assert row["received_day"] == date(2026, 10, 18)
    assert row["received_hour"] == 13
    assert row["features"] == {"launchAtLogin": True, "hideDividers": False}
    assert row["settings"]["iconCount"] == "7"
    columns = {c["column_name"] for c in db._read("SELECT column_name FROM duckdb_columns() WHERE table_name = 'reports'")}
    assert not columns & {"ip", "user_agent", "received_at", "timestamp", "id"}


def test_report_daily_cap(db: Database, clock: Clock) -> None:
    db.max_reports_per_day = 2
    report = validate_report({"schema": 1, "app_version": "0.14.0"})
    db.insert_report(report)
    db.insert_report(report)
    with pytest.raises(CapacityError):
        db.insert_report(report)
    clock.now += timedelta(days=1)
    db.insert_report(report)


# ------------------------------------------------------------------------------------------- hits


def test_hits_are_aggregated_with_an_upsert_counter(db: Database, clock: Clock) -> None:
    hit = {"path": "/icey/", "ref": "news.ycombinator.com", "event": "pageview"}
    for _ in range(3):
        db.record_hit(hit)
    db.record_hit({**hit, "event": "download"})
    clock.now += timedelta(days=1)
    db.record_hit(hit)
    rows = db._read("SELECT day, event, count FROM page_hits ORDER BY day, event")
    assert [(r["day"], r["event"], r["count"]) for r in rows] == [
        (date(2026, 10, 18), "download", 1),
        (date(2026, 10, 18), "pageview", 3),
        (date(2026, 10, 19), "pageview", 1),
    ]


def test_new_hit_rows_fold_into_overflow_when_capped(db: Database) -> None:
    db.max_hit_rows_per_day = 2
    db.record_hit({"path": "/icey/a", "ref": "", "event": "pageview"})
    db.record_hit({"path": "/icey/b", "ref": "", "event": "pageview"})
    db.record_hit({"path": "/icey/c", "ref": "", "event": "pageview"})
    db.record_hit({"path": "/icey/d", "ref": "x.com", "event": "pageview"})
    db.record_hit({"path": "/icey/a", "ref": "", "event": "pageview"})  # existing rows keep counting
    rows = {(r["path"], r["ref"]): r["count"] for r in db._read("SELECT * FROM page_hits")}
    assert rows == {("/icey/a", ""): 2, ("/icey/b", ""): 1, ("(other)", "(other)"): 2}


# ----------------------------------------------------------------------------------------- github


def test_github_ingest_and_last_write_wins(db: Database) -> None:
    parsed, raw = snapshot()
    db.ingest_github(parsed, raw)
    later, raw_later = snapshot(
        collected_at="2026-10-18T18:00:00Z",
        repo={"stars": 125, "forks": 4, "watchers": 3, "open_issues": 6},
        releases=[{"tag": "v0.14.0", "published_at": None, "assets": [{"name": "Icey.dmg", "download_count": 60}]}],
        traffic={
            "views": {"count": 1, "uniques": 1, "views": [{"timestamp": "2026-10-17T00:00:00Z", "count": 22, "uniques": 8},
                                                           {"timestamp": "2026-10-18T00:00:00Z", "count": 4, "uniques": 2}]},
            "referrers": [{"referrer": "github.com", "count": 2, "uniques": 1}],
        },
    )
    db.ingest_github(later, raw_later)

    assert db._read("SELECT count(*) AS n FROM github_snapshots")[0]["n"] == 2
    assert db._read("SELECT stars, open_issues FROM repo_daily")[0] == {"stars": 125, "open_issues": 6}
    downloads = db._read("SELECT tag, asset, download_count FROM release_downloads ORDER BY tag, asset")
    assert downloads == [{"tag": "v0.14.0", "asset": "Icey.dmg", "download_count": 60}]
    traffic = {(r["day"], r["kind"]): (r["count"], r["uniques"]) for r in db._read("SELECT * FROM traffic_daily")}
    assert traffic == {
        (date(2026, 10, 16), "views"): (10, 5),  # kept: the rolling window is upserted, never replaced
        (date(2026, 10, 17), "views"): (22, 8),
        (date(2026, 10, 18), "views"): (4, 2),
        (date(2026, 10, 17), "clones"): (3, 2),
    }
    assert [r["referrer"] for r in db._read("SELECT referrer FROM referrers_daily")] == ["github.com"]
    # The later snapshot had no "paths" section, so the earlier paths for the day are kept.
    assert db._read("SELECT count(*) AS n FROM paths_daily")[0]["n"] == 1
    payload = db._read("SELECT payload->>'$.repository' AS repo FROM github_snapshots LIMIT 1")[0]
    assert payload["repo"] == "leonardovida/icey"


def test_github_ingest_is_atomic(db: Database) -> None:
    parsed, raw = snapshot()
    parsed["paths"] = {"/x": ("t", 1, 1), "/y": (None, 1, 1)}  # NULL title violates NOT NULL mid-transaction
    with pytest.raises(duckdb.Error):
        db.ingest_github(parsed, raw)
    assert db._read("SELECT count(*) AS n FROM github_snapshots")[0]["n"] == 0
    assert db._read("SELECT count(*) AS n FROM repo_daily")[0]["n"] == 0


# ---------------------------------------------------------------------------------------- summary


def test_summary(db: Database) -> None:
    db.insert_report(validate_report(full_report()))
    db.insert_report(validate_report({**full_report(), "features": {"launchAtLogin": False}}))
    db.record_hit({"path": "/icey/", "ref": "", "event": "pageview"})
    parsed, raw = snapshot()
    db.ingest_github(parsed, raw)
    out = db.summary()
    assert out["reports_per_day"] == [{"day": date(2026, 10, 18), "reports": 2}]
    adoption = {r["feature"]: r["pct_enabled"] for r in out["feature_adoption"]}
    assert adoption == {"hideDividers": 0.0, "launchAtLogin": 50.0}
    assert out["os_versions"][0] == {"value": "26.5.1", "reports": 2}
    assert {r["tag"]: r["downloads"] for r in out["downloads_per_release"]} == {"v0.14.0": 55, "v0.13.0": 80}
    assert out["repo"]["stars"] == 120
    assert out["page_hits_per_day"][0]["hits"] == 1


# ------------------------------------------------------------------------------- read-only queries


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  select count(*) from reports",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "DESCRIBE reports",
        "SUMMARIZE reports",
        "SHOW TABLES",
        "-- a comment\nSELECT 1",
        "/* block */ SELECT 1;",
        "(SELECT 1)",
    ],
)
def test_readonly_queries_are_accepted(db: Database, sql: str) -> None:
    assert "rows" in db.run_readonly_query(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        None,
        "DELETE FROM reports",
        "DROP TABLE reports",
        "INSERT INTO reports SELECT * FROM reports",
        "UPDATE page_hits SET count = 0",
        "CREATE TABLE x AS SELECT 1",
        "SELECT 1; DROP TABLE reports",
        "SELECT 1; SELECT 2",
        "PRAGMA database_list",
        "CALL pragma_version()",
        "ATTACH '/tmp/x.duckdb'",
        "COPY reports TO '/tmp/x.csv'",
        "EXPORT DATABASE '/tmp/x'",
        "SET enable_external_access = true",
        "INSTALL httpfs",
        "LOAD httpfs",
        "CHECKPOINT",
        "EXPLAIN ANALYZE SELECT 1",
        "-- SELECT\nDELETE FROM reports",
        "/* SELECT */ DELETE FROM reports",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_text('/proc/self/environ')",
        "SELECT * FROM glob('/data/*')",
        "SELECT 1 FROM",  # parse error
        "SELECT nextval('x')",
    ],
)
def test_non_readonly_queries_are_rejected(db: Database, sql: object) -> None:
    before = db._read("SELECT count(*) AS n FROM reports")[0]["n"]
    with pytest.raises(QueryRejected):
        db.run_readonly_query(sql)  # type: ignore[arg-type]
    assert db._read("SELECT count(*) AS n FROM reports")[0]["n"] == before
    assert db.schema_version() == LATEST_SCHEMA_VERSION


def test_external_access_is_disabled_and_locked(db: Database) -> None:
    with pytest.raises(QueryRejected, match="disabled|Permission"):
        db.run_readonly_query("SELECT * FROM read_csv('/etc/hosts')")
    setting = db.run_readonly_query("SELECT current_setting('enable_external_access') AS v")
    assert setting["rows"] == [[False]]
    assert db.run_readonly_query("SELECT current_setting('lock_configuration')")["rows"] == [[True]]


def test_query_row_cap(db: Database) -> None:
    db.query_row_cap = 5
    out = db.run_readonly_query("SELECT * FROM range(100)")
    assert len(out["rows"]) == 5 and out["truncated"] is True
    assert db.run_readonly_query("SELECT * FROM range(5)")["truncated"] is False


def test_query_timeout(db: Database) -> None:
    db.query_timeout_seconds = 0.3
    with pytest.raises(QueryRejected, match="cancelled"):
        db.run_readonly_query("SELECT count(*) FROM range(100000000000) a, range(10) b")
    assert db.run_readonly_query("SELECT 42")["rows"] == [[42]]  # the slot and connection are usable again


# ---------------------------------------------------------------------------------------- backups


def test_backup_is_a_consistent_copy(db: Database, tmp_path) -> None:
    db.insert_report(validate_report(full_report()))
    path = db.backup()
    assert os.path.basename(path) == "icey-stats-20261018.duckdb"
    db.insert_report(validate_report(full_report()))  # after the copy: must not appear in it
    copy = duckdb.connect(path, read_only=True)
    assert copy.execute("SELECT count(*) FROM reports").fetchone() == (1,)
    copy.close()
    assert db.backup_exists()


def test_backup_rotation_keeps_the_newest(tmp_path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    start = date(2026, 9, 1)
    for i in range(20):
        (backup_dir / f"icey-stats-{start + timedelta(days=i):%Y%m%d}.duckdb").write_bytes(b"x")
    (backup_dir / "icey-stats-20260901.duckdb.wal").write_bytes(b"w")
    (backup_dir / "icey-stats-20261001.duckdb.partial").write_bytes(b"p")
    (backup_dir / "unrelated.txt").write_bytes(b"keep me")
    removed = rotate_backups(str(backup_dir), 14)
    left = sorted(os.listdir(backup_dir))
    assert len([n for n in left if n.endswith(".duckdb")]) == 14
    assert left[0] == "icey-stats-20260907.duckdb"
    assert "unrelated.txt" in left
    assert "icey-stats-20260901.duckdb.wal" in removed and "icey-stats-20261001.duckdb.partial" in removed


def test_backup_rotates_after_writing(db: Database, clock: Clock) -> None:
    db.backup_keep = 3
    for _ in range(5):
        db.backup()
        clock.now += timedelta(days=1)
    names = sorted(n for n in os.listdir(db.backup_dir) if n.endswith(".duckdb"))
    assert names == ["icey-stats-20261020.duckdb", "icey-stats-20261021.duckdb", "icey-stats-20261022.duckdb"]
