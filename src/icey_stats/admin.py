"""Admin client for the loopback-only admin listener. Run it inside the container:

    python -m icey_stats.admin summary [--days 30]
    python -m icey_stats.admin query [--json] [SQL]      # SQL from the argument or stdin
    python -m icey_stats.admin backup
    python -m icey_stats.admin healthcheck               # used by the Docker HEALTHCHECK

There is no admin token: the admin listener is reachable only from inside the container.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

ADMIN_URL = f"http://127.0.0.1:{os.environ.get('ICEY_STATS_ADMIN_PORT', '8081')}"
PUBLIC_URL = f"http://127.0.0.1:{os.environ.get('ICEY_STATS_PORT', '8080')}"


def _request(method: str, url: str, body: bytes | None = None, content_type: str | None = None, timeout: float = 30) -> Any:
    request = urllib.request.Request(url, data=body, method=method)
    if content_type:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", exc.reason)
        except ValueError:
            message = exc.reason
        raise SystemExit(f"error: {message}") from None


def _cell(value: Any) -> str:
    text = "NULL" if value is None else (json.dumps(value) if isinstance(value, dict | list) else str(value))
    text = text.replace("\n", " ")
    return text if len(text) <= 60 else text[:57] + "..."


def print_table(columns: list[str], rows: list[list[Any]]) -> None:
    cells = [[_cell(v) for v in row] for row in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m icey_stats.admin")
    sub = parser.add_subparsers(dest="command", required=True)
    summary = sub.add_parser("summary", help="aggregate statistics as JSON")
    summary.add_argument("--days", type=int, default=30)
    query = sub.add_parser("query", help="run one read-only SQL statement")
    query.add_argument("sql", nargs="?", help="SQL text; read from stdin when omitted or '-'")
    query.add_argument("--json", action="store_true", help="print the raw JSON result")
    sub.add_parser("backup", help="write today's backup now")
    sub.add_parser("healthcheck", help="exit 0 when the public listener answers /healthz")
    args = parser.parse_args(argv)

    if args.command == "healthcheck":
        try:
            with urllib.request.urlopen(f"{PUBLIC_URL}/healthz", timeout=3) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1
    if args.command == "summary":
        print(json.dumps(_request("GET", f"{ADMIN_URL}/summary?days={args.days}"), indent=2))
        return 0
    if args.command == "backup":
        print(json.dumps(_request("POST", f"{ADMIN_URL}/backup", b"", timeout=300)))
        return 0
    sql = sys.stdin.read() if args.sql in (None, "-") else args.sql
    result = _request("POST", f"{ADMIN_URL}/query", json.dumps({"sql": sql}).encode(), "application/json")
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_table(result["columns"], result["rows"])
        if result.get("truncated"):
            print(f"(truncated to {len(result['rows'])} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
