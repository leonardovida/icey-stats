"""Runtime configuration, read once from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

MIN_TOKEN_LENGTH = 32
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._~+/=-]+$")


class ConfigError(Exception):
    """Raised when the environment is not usable. The message never contains secret values."""


@dataclass(frozen=True)
class Settings:
    ingest_token: str
    db_path: str = "/data/icey-stats.duckdb"
    backup_dir: str = "/data/backups"
    backup_keep: int = 14
    public_host: str = "0.0.0.0"
    public_port: int = 8080
    admin_port: int = 8081
    cors_origin: str = "https://leonardovida.github.io"
    github_repository: str = "leonardovida/icey"
    duckdb_memory_limit: str = "96MB"
    duckdb_threads: int = 1
    query_timeout_seconds: float = 10.0
    query_row_cap: int = 1000
    max_reports_per_day: int = 50_000
    max_hit_rows_per_day: int = 2_000

    def __repr__(self) -> str:  # never leak the token through logs or tracebacks
        return f"Settings(db_path={self.db_path!r}, public_port={self.public_port}, admin_port={self.admin_port})"


def check_token(token: str | None) -> str:
    if not token:
        raise ConfigError("ICEY_STATS_INGEST_TOKEN is required")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ConfigError(f"ICEY_STATS_INGEST_TOKEN must have at least {MIN_TOKEN_LENGTH} characters")
    if not _TOKEN_CHARS.match(token):
        raise ConfigError("ICEY_STATS_INGEST_TOKEN contains unsupported characters")
    return token


def _int(env: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    token = check_token(env.get("ICEY_STATS_INGEST_TOKEN"))
    db_path = env.get("ICEY_STATS_DB_PATH") or "/data/icey-stats.duckdb"
    backup_dir = env.get("ICEY_STATS_BACKUP_DIR") or os.path.join(os.path.dirname(db_path), "backups")
    return Settings(
        ingest_token=token,
        db_path=db_path,
        backup_dir=backup_dir,
        backup_keep=_int(env, "ICEY_STATS_BACKUP_KEEP", 14, 1, 365),
        public_port=_int(env, "ICEY_STATS_PORT", 8080, 1, 65535),
        admin_port=_int(env, "ICEY_STATS_ADMIN_PORT", 8081, 1, 65535),
        cors_origin=env.get("ICEY_STATS_CORS_ORIGIN") or "https://leonardovida.github.io",
        github_repository=env.get("ICEY_STATS_GITHUB_REPOSITORY") or "leonardovida/icey",
        duckdb_memory_limit=env.get("ICEY_STATS_DUCKDB_MEMORY_LIMIT") or "96MB",
        duckdb_threads=_int(env, "ICEY_STATS_DUCKDB_THREADS", 1, 1, 8),
    )
