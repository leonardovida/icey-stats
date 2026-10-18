"""Process entrypoint: one process, one DuckDB connection, two listeners.

    python -m icey_stats

The public listener (0.0.0.0:8080) is what Traefik routes to. The admin listener binds 127.0.0.1:8081
inside the container, so only `docker exec` can reach it. SIGTERM stops both listeners gracefully,
writes a final backup and closes the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Iterator

import uvicorn

from .app import build_admin_app, build_public_app
from .config import ConfigError, Settings, load_settings
from .db import Database
from .logs import configure_logging

log = logging.getLogger("icey_stats")
BACKUP_CHECK_SECONDS = 900


class Server(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the process, which runs two of them."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _config(app: object, host: str, port: int) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="off",
        log_config=None,
        access_log=False,  # access logs would contain client addresses
        proxy_headers=False,  # the service never looks at client addresses
        server_header=False,
        limit_concurrency=64,
        backlog=128,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=10,
        h11_max_incomplete_event_size=16 * 1024,
        http="h11",
        loop="asyncio",
        ws="none",
    )


async def backup_loop(db: Database, stop: asyncio.Event) -> None:
    """Write today's backup if it does not exist yet; check every 15 minutes."""
    while not stop.is_set():
        try:
            if not db.backup_exists():
                await asyncio.to_thread(db.backup)
        except Exception:
            log.exception("scheduled backup failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=BACKUP_CHECK_SECONDS)


async def serve(db: Database, settings: Settings) -> None:
    servers = [
        Server(_config(build_public_app(db, settings), settings.public_host, settings.public_port)),
        Server(_config(build_admin_app(db), "127.0.0.1", settings.admin_port)),
    ]
    stop = asyncio.Event()

    def request_stop() -> None:
        if not stop.is_set():
            log.info("shutdown requested")
        stop.set()
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request_stop)

    backups = asyncio.create_task(backup_loop(db, stop))
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    # If either listener stops (or fails to start), stop the other one too.
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    request_stop()
    await asyncio.gather(*tasks, return_exceptions=True)
    backups.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await backups


def main() -> int:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        log.error("invalid configuration", extra={"fields": {"reason": str(exc)}})
        return 2
    db = Database(
        settings.db_path,
        backup_dir=settings.backup_dir,
        backup_keep=settings.backup_keep,
        memory_limit=settings.duckdb_memory_limit,
        threads=settings.duckdb_threads,
        max_reports_per_day=settings.max_reports_per_day,
        max_hit_rows_per_day=settings.max_hit_rows_per_day,
        query_timeout_seconds=settings.query_timeout_seconds,
        query_row_cap=settings.query_row_cap,
    )
    log.info("database ready", extra={"fields": {"schema_version": db.schema_version()}})
    try:
        asyncio.run(serve(db, settings))
        try:
            db.backup()
        except Exception:
            log.exception("final backup failed")
            return 1
    finally:
        db.close()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
