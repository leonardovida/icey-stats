from __future__ import annotations

import json
import logging

import pytest
from starlette.testclient import TestClient

from icey_stats.config import ConfigError, load_settings
from icey_stats.db import Database

from .conftest import TOKEN, full_report, github_snapshot

ORIGIN = "https://leonardovida.github.io"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def count(db: Database, table: str) -> int:
    return db._read(f"SELECT count(*) AS n FROM {table}")[0]["n"]


# ----------------------------------------------------------------------------------------- report


def test_report_accepted(client: TestClient, db: Database) -> None:
    response = client.post("/v1/report", json=full_report())
    assert response.status_code == 204
    assert count(db, "reports") == 1


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"schema": 1}', b"\xff\xfe", b"[" * 5000 + b"]" * 5000])
def test_report_garbage_is_400(client: TestClient, db: Database, body: bytes) -> None:
    response = client.post("/v1/report", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code in (400, 413)
    assert count(db, "reports") == 0


def test_report_requires_json_content_type(client: TestClient) -> None:
    response = client.post("/v1/report", content=json.dumps(full_report()), headers={"Content-Type": "text/plain"})
    assert response.status_code == 415


def test_report_size_limit_by_content_length(client: TestClient, db: Database) -> None:
    body = json.dumps({**full_report(), "padding": "x" * 5000})
    response = client.post("/v1/report", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert count(db, "reports") == 0


def test_report_size_limit_when_streamed(client: TestClient) -> None:
    def chunks():
        for _ in range(10):
            yield b"x" * 1000

    response = client.post("/v1/report", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_report_rejects_invalid_content_length(client: TestClient) -> None:
    response = client.post("/v1/report", content=b"{}", headers={"Content-Type": "application/json", "Content-Length": "-1"})
    assert response.status_code == 400


def test_report_only_post(client: TestClient) -> None:
    assert client.get("/v1/report").status_code == 405


# -------------------------------------------------------------------------------------------- hit


def test_beacon_hit_with_text_plain(client: TestClient, db: Database) -> None:
    body = json.dumps({"path": "/icey/download/", "ref": "news.ycombinator.com", "event": "download"})
    response = client.post("/v1/hit", content=body, headers={"Content-Type": "text/plain;charset=UTF-8", "Origin": ORIGIN})
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    assert db._read("SELECT path, ref, event, count FROM page_hits") == [
        {"path": "/icey/download/", "ref": "news.ycombinator.com", "event": "download", "count": 1}
    ]


def test_hit_from_foreign_origin_is_refused(client: TestClient, db: Database) -> None:
    response = client.post("/v1/hit", json={"path": "/icey/"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers
    assert count(db, "page_hits") == 0


def test_hit_preflight(client: TestClient) -> None:
    headers = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    response = client.options("/v1/hit", headers=headers)
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in response.headers
    denied = client.options("/v1/hit", headers={**headers, "Origin": "https://leonardovida.github.io.evil.example"})
    assert denied.status_code == 403
    assert "access-control-allow-origin" not in denied.headers


def test_hit_get_fallback(client: TestClient, db: Database) -> None:
    response = client.get("/v1/hit", params={"path": "/icey/", "ref": "", "event": "pageview"})
    assert response.status_code == 204
    assert count(db, "page_hits") == 1


@pytest.mark.parametrize(
    "body", [{"path": "/other/"}, {"path": "/icey/", "ref": "https://x.com/a"}, {"path": "/icey/", "event": "click"}]
)
def test_invalid_hit_is_400_with_cors(client: TestClient, db: Database, body: dict) -> None:
    response = client.post("/v1/hit", json=body, headers={"Origin": ORIGIN})
    assert response.status_code == 400
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert count(db, "page_hits") == 0


def test_hit_size_limit(client: TestClient) -> None:
    response = client.post("/v1/hit", content=json.dumps({"path": "/icey/", "x": "y" * 2000}),
                           headers={"Content-Type": "text/plain"})
    assert response.status_code == 413


def test_hit_content_type(client: TestClient) -> None:
    response = client.post("/v1/hit", content=b"path=/icey/", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 415


# ----------------------------------------------------------------------------------------- github


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}, {"Authorization": TOKEN},
     {"Authorization": f"Bearer {TOKEN}x"}],
)
def test_github_requires_the_token(client: TestClient, db: Database, headers: dict) -> None:
    response = client.post("/v1/github", json=github_snapshot(), headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert count(db, "github_snapshots") == 0


def test_github_unauthenticated_large_body_is_401_not_413(client: TestClient) -> None:
    response = client.post("/v1/github", content=b"x" * (2 * 1024 * 1024), headers={"Content-Type": "application/json"})
    assert response.status_code == 401


def test_github_ingest(client: TestClient, db: Database) -> None:
    response = client.post("/v1/github", json=github_snapshot(collected_at="2026-10-18T06:00:00+00:00"), headers=AUTH)
    assert response.status_code == 204, response.text
    assert count(db, "github_snapshots") == 1
    assert count(db, "release_downloads") == 3


def test_github_size_limit(client: TestClient) -> None:
    big = github_snapshot(padding="x" * (1024 * 1024))
    response = client.post("/v1/github", json=big, headers=AUTH)
    assert response.status_code == 413


def test_github_validation_error(client: TestClient) -> None:
    response = client.post("/v1/github", json=github_snapshot(repository="other/repo"), headers=AUTH)
    assert response.status_code == 400


# ------------------------------------------------------------------------------------ misc, admin


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200 and response.text == "ok"


def test_admin_routes_are_not_public(client: TestClient) -> None:
    assert client.get("/summary").status_code == 404
    assert client.post("/query", json={"sql": "SELECT 1"}).status_code == 404
    assert client.post("/backup").status_code == 404


def test_admin_summary_and_query(client: TestClient, admin: TestClient) -> None:
    client.post("/v1/report", json=full_report())
    summary = admin.get("/summary").json()
    assert summary["reports_per_day"] == [{"day": "2026-10-18", "reports": 1}]
    result = admin.post("/query", json={"sql": "SELECT model, features FROM reports"}).json()
    assert result["columns"] == ["model", "features"]
    assert result["rows"] == [["Mac15,10", {"launchAtLogin": True, "hideDividers": False}]]
    plain = admin.post("/query", content=b"SELECT 1 AS one", headers={"Content-Type": "text/plain"}).json()
    assert plain["rows"] == [[1]]
    rejected = admin.post("/query", json={"sql": "DELETE FROM reports"})
    assert rejected.status_code == 400


def test_admin_backup(admin: TestClient) -> None:
    response = admin.post("/backup")
    assert response.status_code == 200
    assert response.json()["backup"].endswith("icey-stats-20261018.duckdb")


def test_logs_never_contain_client_addresses(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    client.post("/v1/report", content=b"garbage", headers={"Content-Type": "application/json",
                                                             "User-Agent": "Secret-Agent/1.0",
                                                             "X-Forwarded-For": "203.0.113.9"})
    client.post("/v1/report", json=full_report())
    text = " ".join(f"{r.getMessage()} {getattr(r, 'fields', '')}" for r in caplog.records)
    assert "status" in text  # the failed request was logged
    for secret in ("testclient", "203.0.113.9", "Secret-Agent", "Mac15,10"):
        assert secret not in text


# ----------------------------------------------------------------------------------------- config


@pytest.mark.parametrize("token", [None, "", "short", "x" * 31, "x" * 40 + " y"])
def test_ingest_token_is_required_and_strong(token: str | None) -> None:
    env = {} if token is None else {"ICEY_STATS_INGEST_TOKEN": token}
    with pytest.raises(ConfigError) as info:
        load_settings(env)
    if token:
        assert token not in str(info.value)


def test_settings_repr_hides_the_token() -> None:
    settings = load_settings({"ICEY_STATS_INGEST_TOKEN": TOKEN})
    assert TOKEN not in repr(settings)
    assert settings.backup_dir == "/data/backups"
