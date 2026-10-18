"""Strict validation of every public payload.

Validators take already-decoded JSON and return plain, normalized dictionaries that contain only the
fields the service stores. Unknown top-level fields are dropped. A known field with a wrong type or an
out-of-range value raises ValidationError, which the HTTP layer turns into a 400.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

REPORT_SCHEMA_VERSIONS = frozenset({1})
CPU_ARCHES = frozenset({"arm64", "x86_64"})
HIT_EVENTS = frozenset({"pageview", "download"})

FLAG_KEY = re.compile(r"[a-z][a-zA-Z0-9]{0,40}")
APP_VERSION = re.compile(r"[0-9]{1,4}(\.[0-9]{1,4}){0,3}([-+][0-9A-Za-z.-]{1,20})?")
OS_VERSION = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){0,2}")
MODEL = re.compile(r"[A-Za-z]+[0-9]+,[0-9]+")
LANGUAGE = re.compile(r"[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,4})?")
PRINTABLE_ASCII = re.compile(r"[\x20-\x7E]+")
SETTING_VALUE = re.compile(r"[A-Za-z0-9._:+-]{0,32}")
HIT_PATH = re.compile(r"/icey(/[a-zA-Z0-9._-]*)*/?")
HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}")

MAX_FEATURES = 32
MAX_SETTINGS = 16
MAX_COUNT = 10**12


class ValidationError(ValueError):
    """The payload is not acceptable. The message is safe to return to the client."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_field(data: dict[str, Any], name: str, low: int, high: int) -> int | None:
    if name not in data or data[name] is None:
        return None
    value = data[name]
    if not _is_int(value) or not low <= value <= high:
        raise ValidationError(f"{name} must be an integer between {low} and {high}")
    return value


def _pattern_field(data: dict[str, Any], name: str, pattern: re.Pattern[str], max_len: int) -> str | None:
    if name not in data or data[name] is None:
        return None
    value = data[name]
    if not isinstance(value, str) or len(value) > max_len or not pattern.fullmatch(value):
        raise ValidationError(f"{name} is not valid")
    return value


def _require_object(data: Any, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValidationError(f"{what} must be a JSON object")
    return data


def _printable(value: Any, name: str, max_len: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > max_len or (not value and not allow_empty):
        raise ValidationError(f"{name} is not valid")
    if value and not value.isprintable():
        raise ValidationError(f"{name} is not valid")
    return value


# --------------------------------------------------------------------------------------------- report


def validate_report(data: Any) -> dict[str, Any]:
    data = _require_object(data, "report")
    schema = data.get("schema")
    if not _is_int(schema) or schema not in REPORT_SCHEMA_VERSIONS:
        raise ValidationError("schema must be a supported integer version")
    app_version = _pattern_field(data, "app_version", APP_VERSION, 32)
    if app_version is None:
        raise ValidationError("app_version is required")

    cpu_arch = data.get("cpu_arch")
    if cpu_arch is not None and cpu_arch not in CPU_ARCHES:
        raise ValidationError("cpu_arch must be arm64 or x86_64")

    cpu_brand = data.get("cpu_brand")
    if cpu_brand is not None:
        if not isinstance(cpu_brand, str) or len(cpu_brand) > 64 or not PRINTABLE_ASCII.fullmatch(cpu_brand):
            raise ValidationError("cpu_brand is not valid")
        cpu_brand = cpu_brand.strip() or None

    has_notch = data.get("has_notch")
    if has_notch is not None and not isinstance(has_notch, bool):
        raise ValidationError("has_notch must be a boolean")

    return {
        "schema": schema,
        "app_version": app_version,
        "build": _int_field(data, "build", 0, 2_000_000_000),
        "os_version": _pattern_field(data, "os_version", OS_VERSION, 16),
        "model": _pattern_field(data, "model", MODEL, 32),
        "cpu_arch": cpu_arch,
        "cpu_brand": cpu_brand,
        "cpu_cores": _int_field(data, "cpu_cores", 1, 256),
        "memory_gb": _int_field(data, "memory_gb", 1, 4096),
        "display_count": _int_field(data, "display_count", 0, 16),
        "has_notch": has_notch,
        "language": _pattern_field(data, "language", LANGUAGE, 10),
        "features": _features(data.get("features")),
        "settings": _settings(data.get("settings")),
    }


def _features(value: Any) -> dict[str, bool] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > MAX_FEATURES:
        raise ValidationError(f"features must be an object with at most {MAX_FEATURES} entries")
    out: dict[str, bool] = {}
    for key, flag in value.items():
        if not FLAG_KEY.fullmatch(key) or not isinstance(flag, bool):
            raise ValidationError("features must map camelCase keys to booleans")
        out[key] = flag
    return out


def _settings(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > MAX_SETTINGS:
        raise ValidationError(f"settings must be an object with at most {MAX_SETTINGS} entries")
    out: dict[str, str] = {}
    for key, setting in value.items():
        if not FLAG_KEY.fullmatch(key):
            raise ValidationError("settings keys must be camelCase")
        if isinstance(setting, bool):
            out[key] = "true" if setting else "false"
        elif _is_int(setting) and -1_000_000 <= setting <= 1_000_000:
            out[key] = str(setting)
        elif isinstance(setting, str) and SETTING_VALUE.fullmatch(setting):
            out[key] = setting
        else:
            raise ValidationError("settings values must be short tokens, booleans or small integers")
    return out


# ------------------------------------------------------------------------------------------------ hit


def validate_hit(data: Any) -> dict[str, str]:
    data = _require_object(data, "hit")
    path = data.get("path")
    if not isinstance(path, str) or len(path) > 120 or not HIT_PATH.fullmatch(path):
        raise ValidationError("path is not valid")
    ref = data.get("ref") or ""
    if not isinstance(ref, str):
        raise ValidationError("ref is not valid")
    ref = ref.lower().rstrip(".")
    if ref and (len(ref) > 100 or not HOSTNAME.fullmatch(ref)):
        raise ValidationError("ref must be a hostname")
    event = data.get("event") or "pageview"
    if event not in HIT_EVENTS:
        raise ValidationError("event must be pageview or download")
    return {"path": path, "ref": ref, "event": event}


# --------------------------------------------------------------------------------------------- github


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise ValidationError(f"{name} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a time zone")
    return parsed.astimezone(UTC)


def _count(value: Any, name: str) -> int:
    if not _is_int(value) or not 0 <= value <= MAX_COUNT:
        raise ValidationError(f"{name} must be a non-negative integer")
    return value


def _list(value: Any, name: str, max_items: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValidationError(f"{name} must be a list with at most {max_items} items")
    return value


def validate_github(data: Any, *, expected_repository: str, now: datetime | None = None) -> dict[str, Any]:
    data = _require_object(data, "snapshot")
    now = now or datetime.now(UTC)
    collected_at = _timestamp(data.get("collected_at"), "collected_at")
    if not datetime(2020, 1, 1, tzinfo=UTC) <= collected_at <= now + timedelta(hours=1):
        raise ValidationError("collected_at is out of range")
    repository = data.get("repository")
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise ValidationError("repository must be owner/name")
    if repository.lower() != expected_repository.lower():
        raise ValidationError("repository is not the configured repository")

    repo = _require_object(data.get("repo"), "repo")
    repo_counts = {key: _count(repo.get(key), f"repo.{key}") for key in ("stars", "forks", "watchers", "open_issues")}

    # A section that is absent stays None, so the rows already stored for that day are kept.
    downloads: dict[tuple[str, str], int] = {}
    for release in _list(data.get("releases", []), "releases", 500):
        release = _require_object(release, "release")
        tag = _printable(release.get("tag"), "release.tag", 100)
        if release.get("published_at") is not None:
            _timestamp(release.get("published_at"), "release.published_at")
        for asset in _list(release.get("assets", []), "release.assets", 100):
            asset = _require_object(asset, "asset")
            name = _printable(asset.get("name"), "asset.name", 200)
            downloads[(tag, name)] = _count(asset.get("download_count"), "asset.download_count")

    traffic_rows: dict[tuple[date, str], tuple[int, int]] = {}
    referrers: dict[str, tuple[int, int]] = {}
    paths: dict[str, tuple[str, int, int]] = {}
    traffic = data.get("traffic")
    if traffic is not None:
        traffic = _require_object(traffic, "traffic")
        for kind in ("views", "clones"):
            block = traffic.get(kind)
            if block is None:
                continue
            block = _require_object(block, f"traffic.{kind}")
            # GitHub nests the daily list under the kind name ("views" or "clones").
            daily = block.get(kind, block.get("views" if kind == "clones" else "clones", []))
            for item in _list(daily, f"traffic.{kind}.{kind}", 400):
                item = _require_object(item, f"traffic.{kind} item")
                day = _timestamp(item.get("timestamp"), f"traffic.{kind}.timestamp").date()
                traffic_rows[(day, kind)] = (
                    _count(item.get("count"), f"traffic.{kind}.count"),
                    _count(item.get("uniques"), f"traffic.{kind}.uniques"),
                )
        for item in _list(traffic.get("referrers", []), "traffic.referrers", 100):
            item = _require_object(item, "referrer")
            referrer = _printable(item.get("referrer"), "referrer.referrer", 200)
            referrers[referrer] = (_count(item.get("count"), "referrer.count"), _count(item.get("uniques"), "referrer.uniques"))
        for item in _list(traffic.get("paths", []), "traffic.paths", 100):
            item = _require_object(item, "path")
            path = _printable(item.get("path"), "path.path", 300)
            title = _printable(item.get("title") or "", "path.title", 300, allow_empty=True)
            paths[path] = (title, _count(item.get("count"), "path.count"), _count(item.get("uniques"), "path.uniques"))
    traffic_present = isinstance(traffic, dict)

    return {
        "collected_at": collected_at.replace(tzinfo=None),
        "collected_day": collected_at.date(),
        "repository": repository,
        "repo": repo_counts,
        "downloads": downloads if "releases" in data else None,
        "traffic": traffic_rows,
        "referrers": referrers if traffic_present and "referrers" in traffic else None,
        "paths": paths if traffic_present and "paths" in traffic else None,
    }
