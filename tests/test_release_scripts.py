"""Pure helpers of scripts/provision.py and scripts/deploy.py. Nothing here touches SSH, Dokploy or 1Password."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vpslib  # noqa: E402

A, B, C, D, E = ("a" * 40, "b" * 40, "c" * 40, "d" * 40, "e" * 40)


def test_parse_env_text() -> None:
    text = 'A=1\n# comment\nexport B="two words"\nC=\'x=y\'\n\nbroken\nD=\n'
    assert vpslib.parse_env_text(text) == {"A": "1", "B": "two words", "C": "x=y", "D": ""}
    assert vpslib.parse_env_text(None) == {}  # type: ignore[arg-type]


def test_render_env_refuses_newlines() -> None:
    assert vpslib.render_env({"ICEY_STATS_HOST": "h", "ICEY_STATS_IMAGE": "i"}) == "ICEY_STATS_HOST=h\nICEY_STATS_IMAGE=i\n"
    with pytest.raises(SystemExit):
        vpslib.render_env({"ICEY_STATS_INGEST_TOKEN": "abc\nEVIL=1"})
    with pytest.raises(SystemExit):
        vpslib.render_env({"bad key": "x"})


def test_ingest_token_is_kept_once_generated() -> None:
    existing = "ICEY_STATS_IMAGE=icey-stats:x\nICEY_STATS_INGEST_TOKEN=" + "f" * 64 + "\n"
    assert vpslib.ingest_token(existing) == ("f" * 64, False)
    generated, is_new = vpslib.ingest_token("")
    assert is_new and len(generated) == 64 and vpslib.TOKEN.match(generated)
    assert vpslib.ingest_token("ICEY_STATS_INGEST_TOKEN=short")[1] is True
    assert vpslib.ingest_token(None)[1] is True


def test_plan_prune_keeps_newest_and_protected() -> None:
    assert vpslib.plan_prune([A, B, C, D, E], 3, set()) == [D, E]
    assert vpslib.plan_prune([A, B, C, D, E], 3, {E}) == [D]
    assert vpslib.plan_prune([A, "latest", "../etc", B], 3, set()) == []  # only commit-shaped names
    assert vpslib.plan_prune([], 3, {A}) == []


def test_dokploy_name_discovery_is_shape_agnostic() -> None:
    projects = [
        {"projectId": "p1", "name": "torre-dei-beati", "environments": [
            {"environmentId": "e1", "projectId": "p1", "name": "production",
             "compose": [{"composeId": "c1", "name": "torre-dei-beati", "environmentId": "e1"}],
             "applications": [{"applicationId": "a1", "name": "web", "projectId": "p1"}]},
        ]},
        {"projectId": "p2", "name": "openclaw-agent", "environments": []},
    ]
    assert vpslib.project_names(projects) == {"torre-dei-beati", "openclaw-agent"}
    assert vpslib.compose_names(projects) == {"torre-dei-beati"}
    assert vpslib.compose_names({"items": [{"composeId": "x", "name": "icey-stats"}], "total": 1}) == {"icey-stats"}


def test_first_id_reads_nested_and_flat_responses() -> None:
    assert vpslib.first_id({"project": {"projectId": "p9"}, "environment": {"environmentId": "e9"}}, "projectId", "project") == "p9"
    assert vpslib.first_id({"projectId": "p8", "name": "x"}, "projectId", "project") == "p8"
    assert vpslib.first_id(None, "composeId") is None


def test_secrets_are_read_from_the_mount_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mount = tmp_path / "personal-apps.env"
    mount.write_text('VPS_ADMIN__DOKPLOY_AUTH_TOKEN="dk_secret"\nVPS_ADMIN__SSH_SERVER=root@203.0.113.1\nOTHER=1\n')
    monkeypatch.setenv("PERSONAL_APPS_ENV_FILE", str(mount))
    assert vpslib.read_personal_apps_secrets() == {"api_key": "dk_secret", "server": "root@203.0.113.1"}


def test_missing_secrets_abort_without_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PERSONAL_APPS_ENV_FILE", str(tmp_path / "missing"))
    with pytest.raises(SystemExit):
        vpslib.read_personal_apps_secrets()
    mount = tmp_path / "partial.env"
    mount.write_text("VPS_ADMIN__SSH_SERVER=root@203.0.113.1\n")
    monkeypatch.setenv("PERSONAL_APPS_ENV_FILE", str(mount))
    with pytest.raises(SystemExit):
        vpslib.read_personal_apps_secrets()
    assert "203.0.113.1" not in capsys.readouterr().err


def test_target_is_valid_and_unprovisioned() -> None:
    target = vpslib.load_target()
    assert target["repository"] == "leonardovida/icey-stats"
    assert target["server"] == "root@178.105.139.229"
    assert target["hostname"] == "icey-stats.178-105-139-229.sslip.io"
    assert "composeId" in target


@pytest.mark.parametrize("script", ["deploy.py", "provision.py"])
def test_scripts_show_help_without_side_effects(script: str, tmp_path: Path) -> None:
    # Point the secrets mount at a missing file: even a regression here can never read the real one.
    env = {"PATH": "/usr/bin:/bin", "PERSONAL_APPS_ENV_FILE": str(tmp_path / "missing")}
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script), "--help"],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


@pytest.mark.parametrize("script", ["query", "summary", "_remote-admin"])
def test_bin_scripts_parse(script: str) -> None:
    subprocess.run(["sh", "-n", str(ROOT / "bin" / script)], check=True)
