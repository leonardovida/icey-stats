"""Guards for the release artefacts: hashed requirements in sync, strict container profile kept."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_requirements_lock_matches_uv_lock() -> None:
    # Compare stdout rather than writing in place, so drift fails the test instead of being fixed silently.
    result = subprocess.run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements-txt", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    committed = (ROOT / "requirements.lock").read_text()
    body = "\n".join(line for line in committed.splitlines() if not line.startswith("#")).strip()
    assert result.stdout.strip() == body, "run: uv export --frozen --no-dev --no-emit-project -o requirements.lock"
    assert "--hash=sha256:" in committed


def test_dockerfile_pins_the_base_image_and_runs_unprivileged() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert re.search(r"python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}", dockerfile)
    assert "--require-hashes" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "org.opencontainers.image.revision=$RELEASE_COMMIT" in dockerfile


def test_compose_keeps_the_strict_profile() -> None:
    compose = (ROOT / "deploy" / "compose.yml").read_text()
    for required in [
        'user: "10001:10001"',
        "read_only: true",
        "- ALL",
        "no-new-privileges:true",
        "mem_limit: 256m",
        "cpus: 0.5",
        "pids_limit: 128",
        "${ICEY_STATS_IMAGE:?",
        "${ICEY_STATS_INGEST_TOKEN:?",
        "${ICEY_STATS_HOST:?",
        "tls.certresolver: letsencrypt",
        "ratelimit.average",
        "external: true",
    ]:
        assert required in compose, required
    assert "ports:" not in compose, "the service must not publish host ports"
    assert "8081" not in compose.split("expose:", 1)[1].split("environment:", 1)[0]
    assert "docker.sock" not in compose
