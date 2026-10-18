from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from icey_stats.app import build_admin_app, build_public_app
from icey_stats.config import Settings
from icey_stats.db import Database

TOKEN = "t" * 40
NOW = datetime(2026, 10, 18, 13, 47, 12, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "icey-stats.duckdb")


@pytest.fixture
def db(db_path: str, clock: Clock) -> Iterator[Database]:
    database = Database(db_path, clock=clock)
    yield database
    database.close()


@pytest.fixture
def settings(db_path: str) -> Settings:
    return Settings(ingest_token=TOKEN, db_path=db_path)


@pytest.fixture
def client(db: Database, settings: Settings) -> TestClient:
    return TestClient(build_public_app(db, settings))


@pytest.fixture
def admin(db: Database) -> TestClient:
    return TestClient(build_admin_app(db))


def full_report() -> dict:
    return {
        "schema": 1,
        "app_version": "0.14.0",
        "build": 140,
        "os_version": "26.5.1",
        "model": "Mac15,10",
        "cpu_arch": "arm64",
        "cpu_brand": "Apple M3 Max",
        "cpu_cores": 14,
        "memory_gb": 36,
        "display_count": 2,
        "has_notch": True,
        "language": "pt-BR",
        "features": {"launchAtLogin": True, "hideDividers": False},
        "settings": {"spacing": "compact", "iconCount": 7, "autoRehide": True},
    }


def github_snapshot(**overrides: object) -> dict:
    data = {
        "collected_at": "2026-10-18T06:00:00Z",
        "repository": "leonardovida/icey",
        "repo": {"stars": 120, "forks": 4, "watchers": 3, "open_issues": 7},
        "releases": [
            {
                "tag": "v0.14.0",
                "published_at": "2026-10-01T10:00:00Z",
                "assets": [{"name": "Icey.dmg", "download_count": 50}, {"name": "Icey.zip", "download_count": 5}],
            },
            {"tag": "v0.13.0", "published_at": "2026-09-01T10:00:00Z", "assets": [{"name": "Icey.dmg", "download_count": 80}]},
        ],
        "traffic": {
            "views": {
                "count": 30,
                "uniques": 12,
                "views": [
                    {"timestamp": "2026-10-16T00:00:00Z", "count": 10, "uniques": 5},
                    {"timestamp": "2026-10-17T00:00:00Z", "count": 20, "uniques": 7},
                ],
            },
            "clones": {"count": 3, "uniques": 2, "clones": [{"timestamp": "2026-10-17T00:00:00Z", "count": 3, "uniques": 2}]},
            "referrers": [{"referrer": "news.ycombinator.com", "count": 9, "uniques": 6}],
            "paths": [{"path": "/leonardovida/icey", "title": "leonardovida/icey: menu bar", "count": 25, "uniques": 10}],
        },
    }
    data.update(overrides)
    return data
