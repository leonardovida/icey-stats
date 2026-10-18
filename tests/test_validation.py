from __future__ import annotations

from datetime import date

import pytest

from icey_stats.validation import ValidationError, validate_github, validate_hit, validate_report

from .conftest import NOW, full_report, github_snapshot


def report(**overrides: object) -> dict:
    data = full_report()
    data.update(overrides)
    return data


def test_full_report_is_normalized() -> None:
    out = validate_report(full_report())
    assert out["model"] == "Mac15,10"
    assert out["features"] == {"launchAtLogin": True, "hideDividers": False}
    assert out["settings"] == {"spacing": "compact", "iconCount": "7", "autoRehide": "true"}


def test_minimal_report_only_needs_schema_and_version() -> None:
    out = validate_report({"schema": 1, "app_version": "0.14.0"})
    assert out["app_version"] == "0.14.0"
    assert out["model"] is None and out["features"] is None


def test_unknown_fields_are_dropped() -> None:
    out = validate_report(report(serial_number="C02XYZ", ip="1.2.3.4", hostname="leo-mbp"))
    assert "serial_number" not in out and "ip" not in out and "hostname" not in out


@pytest.mark.parametrize("payload", [None, [], "x", 1, {"app_version": "0.14.0"}])
def test_garbage_is_rejected(payload: object) -> None:
    with pytest.raises(ValidationError):
        validate_report(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", 2),
        ("schema", True),
        ("schema", "1"),
        ("app_version", None),
        ("app_version", "latest"),
        ("app_version", "1.2.3.4.5"),
        ("app_version", "0.14.0" + "x" * 40),
    ],
)
def test_invalid_required_fields_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        validate_report(report(**{field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("build", -1),
        ("build", 1.5),
        ("build", "140"),
        ("os_version", "26.5.1.2"),
        ("os_version", "macOS 26"),
        ("model", "Mac15"),
        ("model", "Mac15,10; rm -rf"),
        ("model", "15,10"),
        ("cpu_arch", "ppc"),
        ("cpu_brand", "x" * 65),
        ("cpu_brand", "Apple\nM3"),
        ("cpu_brand", "Apple M3 é"),
        ("cpu_cores", 0),
        ("cpu_cores", 257),
        ("cpu_cores", True),
        ("memory_gb", 0),
        ("memory_gb", 4097),
        ("memory_gb", 16.0),
        ("display_count", -1),
        ("display_count", 17),
        ("has_notch", "yes"),
        ("has_notch", 1),
        ("language", "english"),
        ("language", "pt_BR"),
        ("language", "en-US-x-private"),
        ("features", []),
        ("features", "on"),
        ("settings", []),
    ],
)
def test_invalid_optional_fields_are_dropped(field: str, value: object) -> None:
    out = validate_report(report(**{field: value}))
    assert out[field] is None
    assert out["app_version"] == "0.14.0"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("features", {"LaunchAtLogin": True, "useIceBar": True}, {"useIceBar": True}),
        ("features", {"launch_at_login": True}, {}),
        ("features", {"launchAtLogin": "true"}, {}),
        ("features", {f"flag{i}": True for i in range(33)}, {f"flag{i}": True for i in range(32)}),
        ("features", {"a" + "b" * 41: True}, {}),
        ("settings", {"spacing": "x" * 33, "rehide": "smart"}, {"rehide": "smart"}),
        ("settings", {"spacing": "has space"}, {}),
        ("settings", {"spacing": 10**7}, {}),
        ("settings", {"spacing": None}, {}),
        ("settings", {"spacing": ["a"]}, {}),
        ("settings", {f"s{i}": "a" for i in range(17)}, {f"s{i}": "a" for i in range(16)}),
    ],
)
def test_invalid_entries_are_dropped(field: str, value: object, expected: dict) -> None:
    assert validate_report(report(**{field: value}))[field] == expected


@pytest.mark.parametrize("model", ["Mac15,10", "VirtualMac2,1", "MacBookPro18,3", "iMac21,1", "Macmini9,1"])
def test_models(model: str) -> None:
    assert validate_report(report(model=model))["model"] == model


@pytest.mark.parametrize("language", ["en", "pt-BR", "zh-Hans", "yue"])
def test_languages(language: str) -> None:
    assert validate_report(report(language=language))["language"] == language


def test_bounds_are_inclusive() -> None:
    out = validate_report(report(cpu_cores=256, memory_gb=4096, display_count=0,
                                 features={f"flag{i}": True for i in range(32)}))
    assert out["cpu_cores"] == 256 and out["memory_gb"] == 4096 and out["display_count"] == 0
    assert len(out["features"]) == 32


def test_cpu_brand_is_trimmed() -> None:
    assert validate_report(report(cpu_brand="  Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz "))["cpu_brand"] == (
        "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz"
    )


# --------------------------------------------------------------------------------------------- hits


@pytest.mark.parametrize("path", ["/icey", "/icey/", "/icey/download/", "/icey/docs/setup.html", "/icey/a/b_c-d.e/"])
def test_valid_hit_paths(path: str) -> None:
    assert validate_hit({"path": path})["path"] == path


@pytest.mark.parametrize(
    "path", ["/", "/other/", "/iceyx/", "icey/", "/icey//x?", "/icey/<script>", "/icey/" + "a" * 120, None, 3, "/icey/é"]
)
def test_invalid_hit_paths(path: object) -> None:
    with pytest.raises(ValidationError):
        validate_hit({"path": path})


def test_hit_defaults_and_normalization() -> None:
    assert validate_hit({"path": "/icey/"}) == {"path": "/icey/", "ref": "", "event": "pageview"}
    assert validate_hit({"path": "/icey/", "ref": "News.YCombinator.com.", "event": "download"}) == {
        "path": "/icey/", "ref": "news.ycombinator.com", "event": "download"}


@pytest.mark.parametrize("ref", ["https://news.ycombinator.com/item?id=1", "news ycombinator", "a" * 101, "-bad.com", 5, "x_y.com"])
def test_invalid_refs(ref: object) -> None:
    with pytest.raises(ValidationError):
        validate_hit({"path": "/icey/", "ref": ref})


@pytest.mark.parametrize("event", ["click", "PAGEVIEW", 1])
def test_invalid_events(event: object) -> None:
    with pytest.raises(ValidationError):
        validate_hit({"path": "/icey/", "event": event})


# ------------------------------------------------------------------------------------------- github


def test_github_snapshot_is_normalized() -> None:
    out = validate_github(github_snapshot(), expected_repository="leonardovida/icey", now=NOW)
    assert out["collected_day"] == date(2026, 10, 18)
    assert out["downloads"] == {("v0.14.0", "Icey.dmg"): 50, ("v0.14.0", "Icey.zip"): 5, ("v0.13.0", "Icey.dmg"): 80}
    assert out["traffic"][(date(2026, 10, 17), "views")] == (20, 7)
    assert out["traffic"][(date(2026, 10, 17), "clones")] == (3, 2)
    assert out["referrers"] == {"news.ycombinator.com": (9, 6)}
    assert out["paths"]["/leonardovida/icey"][1:] == (25, 10)


def test_github_absent_sections_are_none() -> None:
    data = github_snapshot()
    del data["releases"], data["traffic"]
    out = validate_github(data, expected_repository="leonardovida/icey", now=NOW)
    assert out["downloads"] is None and out["referrers"] is None and out["paths"] is None and out["traffic"] == {}


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": "someone/else"},
        {"repository": "not a repo"},
        {"collected_at": "yesterday"},
        {"collected_at": "2026-10-18T06:00:00"},
        {"collected_at": "2019-01-01T00:00:00Z"},
        {"collected_at": "2027-01-01T00:00:00Z"},
        {"repo": {"stars": -1, "forks": 0, "watchers": 0, "open_issues": 0}},
        {"repo": {"stars": 1}},
        {"releases": [{"tag": "", "assets": []}]},
        {"releases": [{"tag": "v1", "assets": [{"name": "a", "download_count": "5"}]}]},
        {"releases": {"tag": "v1"}},
        {"traffic": {"views": {"views": [{"timestamp": "bad", "count": 1, "uniques": 1}]}}},
        {"traffic": {"referrers": [{"referrer": "x", "count": True, "uniques": 1}]}},
    ],
)
def test_invalid_github_snapshots(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        validate_github(github_snapshot(**overrides), expected_repository="leonardovida/icey", now=NOW)
