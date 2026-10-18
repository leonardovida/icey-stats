"""Shared helpers for provision.py and deploy.py. Standard library only.

Secrets come from the 1Password "Personal Apps" mount ($HOME/.config/personal-apps/.env, a FIFO that
can be read once per run), exactly like openclaw-agent's scripts/deploy.zsh. Values are never printed.
The Dokploy panel is private: its API is reached through a short-lived SSH tunnel to 127.0.0.1:3000.

Dokploy endpoints used (REST form of the tRPC router, /api/<router>.<procedure>; input schemas checked
against the OpenAPI-generated tools of @dokploy/mcp 0.30.7, generated 2026-09-18, and @dokploy/cli 0.29.3):
  GET  project.all, environment.byProjectId, compose.one, compose.search
  POST project.create, compose.create, compose.update, compose.deploy
Response shapes are not part of that spec; the helpers below read them defensively.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
APP = "icey-stats"
RELEASES = "/var/lib/icey-stats/releases"
SERVICE = "icey-stats"
TOKEN_NAME = "ICEY_STATS_INGEST_TOKEN"
KEEP_RELEASES = 3

COMMIT = re.compile(r"^[0-9a-f]{40}$")
TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{32,256}$")
SERVER = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$|^[A-Za-z0-9_.-]+$")
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
DOKPLOY_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


class Abort(SystemExit):
    """Stop with a message on stderr. Messages never contain secret values."""

    def __init__(self, message: str) -> None:
        print(f"error: {message}", file=sys.stderr)
        super().__init__(1)


def info(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------------- local state


def load_target() -> dict[str, Any]:
    target = json.loads((ROOT / "deploy" / "target.json").read_text())
    if not HOSTNAME.match(target.get("hostname", "")):
        raise Abort("deploy/target.json hostname is invalid")
    if not SERVER.match(target.get("server", "")):
        raise Abort("deploy/target.json server is invalid")
    if target.get("composeId") is not None and not DOKPLOY_ID.match(str(target["composeId"])):
        raise Abort("deploy/target.json composeId is invalid")
    return target


def git(*args: str, input: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True, input=input).stdout


def assert_commit(value: str) -> str:
    if not COMMIT.match(value or ""):
        raise Abort("use a full 40-character commit SHA")
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines (Dokploy env, 1Password mount). Surrounding quotes are removed."""
    values: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def render_env(values: dict[str, str]) -> str:
    for key, value in values.items():
        if not re.match(r"^[A-Z][A-Z0-9_]*$", key) or any(c in value for c in "\r\n\0"):
            raise Abort(f"refusing to write an invalid environment entry for {key}")
    return "".join(f"{key}={value}\n" for key, value in values.items())


def read_personal_apps_secrets() -> dict[str, str]:
    """Read the Dokploy API key and SSH target from the 1Password mount. Read once: it is a FIFO."""
    path = os.environ.get("PERSONAL_APPS_ENV_FILE") or str(Path.home() / ".config/personal-apps/.env")
    try:
        with open(path, encoding="utf-8") as handle:
            contents = handle.read()
    except OSError:
        raise Abort("the Personal Apps 1Password mount is unavailable") from None
    values = parse_env_text(contents)
    del contents
    api_key = values.get("VPS_ADMIN__DOKPLOY_AUTH_TOKEN", "")
    if not api_key:
        raise Abort("VPS_ADMIN__DOKPLOY_AUTH_TOKEN is missing from the Personal Apps environment")
    return {"api_key": api_key, "server": values.get("VPS_ADMIN__SSH_SERVER", "")}


def ingest_token(existing_env: str | None) -> tuple[str, bool]:
    """Keep the token already stored in Dokploy; generate one only on the first deploy."""
    current = parse_env_text(existing_env or "").get(TOKEN_NAME, "")
    if TOKEN.match(current):
        return current, False
    return secrets.token_hex(32), True


# ---------------------------------------------------------------------------------------- server


class Remote:
    def __init__(self, server: str) -> None:
        if not SERVER.match(server):
            raise Abort("invalid SSH server")
        self.server = server
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", server]

    def run(self, command: str, *, check: bool = True, stream: bool = False, input: str | None = None) -> str:
        result = subprocess.run(
            [*self.ssh, command],
            check=False,
            text=True,
            input=input,
            stdout=None if stream else subprocess.PIPE,
            stderr=None if stream else subprocess.PIPE,
        )
        if check and result.returncode != 0:
            lines = [] if stream else (result.stderr or "").strip().splitlines()
            detail = f": {lines[-1][:200]}" if lines else ""
            raise Abort(f"remote command failed with exit code {result.returncode}{detail}")
        return result.stdout or ""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Dokploy:
    """Dokploy API over an SSH tunnel that lives only as long as the `with` block."""

    def __init__(self, server: str, api_key: str) -> None:
        self.server = server
        self._api_key = api_key
        self.port = int(os.environ.get("DOKPLOY_TUNNEL_PORT") or free_port())
        self._tunnel: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> "Dokploy":
        self._tunnel = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=15", "-N",
             "-L", f"127.0.0.1:{self.port}:127.0.0.1:3000", self.server],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        )
        for _ in range(60):
            if self._tunnel.poll() is not None:
                raise Abort("the Dokploy SSH tunnel failed")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return self
            except OSError:
                time.sleep(0.5)
        raise Abort("the Dokploy SSH tunnel did not become ready")

    def __exit__(self, *exc: object) -> None:
        if self._tunnel and self._tunnel.poll() is None:
            self._tunnel.terminate()
            try:
                self._tunnel.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._tunnel.kill()

    def _call(self, method: str, endpoint: str, body: dict[str, Any] | None = None,
              query: dict[str, Any] | None = None) -> Any:
        url = f"http://127.0.0.1:{self.port}/api/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"x-api-key": self._api_key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            # Only the status: error bodies could echo submitted values such as the env.
            raise Abort(f"Dokploy {endpoint} failed with HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise Abort(f"Dokploy {endpoint} is unreachable") from None
        return json.loads(text) if text.strip() else None

    def get(self, endpoint: str, **query: Any) -> Any:
        return self._call("GET", endpoint, query=query)

    def post(self, endpoint: str, body: dict[str, Any]) -> Any:
        return self._call("POST", endpoint, body=body)


def walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def project_names(payload: Any) -> set[str]:
    """Names of projects anywhere in a Dokploy response (environments also carry a projectId)."""
    return {
        str(item["name"]) for item in walk(payload)
        if "projectId" in item and "name" in item
        and not {"environmentId", "composeId", "applicationId"} & item.keys()
    }


def compose_names(payload: Any) -> set[str]:
    return {str(item["name"]) for item in walk(payload) if "composeId" in item and "name" in item}


def first_id(payload: Any, key: str, nested: str | None = None) -> str | None:
    if isinstance(payload, dict):
        if nested and isinstance(payload.get(nested), dict) and payload[nested].get(key):
            return str(payload[nested][key])
        if payload.get(key):
            return str(payload[key])
    return None


# ----------------------------------------------------------------------------------------- prune


def plan_prune(newest_first: list[str], keep: int, protect: set[str]) -> list[str]:
    """Return the release commits to delete: everything past the newest `keep`, minus `protect`."""
    commits = [c for c in newest_first if COMMIT.match(c)]
    kept = set(commits[:keep]) | protect
    return [c for c in commits if c not in kept]


def prune(remote: Remote, protect: set[str]) -> None:
    """Delete old release directories and images of THIS app only; keep the newest three."""
    images = remote.run(f"docker image ls {APP} --format '{{{{.Tag}}}}'").split()  # newest first
    dirs = remote.run(f"ls -1t {RELEASES} 2>/dev/null || true").split()  # newest first
    for commit in plan_prune(images, KEEP_RELEASES, protect):
        remote.run(f"docker image rm {APP}:{commit} >/dev/null", check=False)
        info(f"pruned image {APP}:{commit[:12]}")
    for commit in plan_prune(dirs, KEEP_RELEASES, protect):
        remote.run(f"rm -rf -- {RELEASES}/{commit}")
        info(f"pruned release directory {commit[:12]}")
