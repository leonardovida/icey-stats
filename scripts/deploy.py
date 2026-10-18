#!/usr/bin/env python3
"""Release icey-stats to the VPS through Dokploy.

    uv run scripts/deploy.py deploy               # HEAD: clean tree, on origin/main, tests pass
    uv run scripts/deploy.py rollback <commit>    # a retained image (the newest three are kept)
    uv run scripts/deploy.py prune                # delete old release dirs and images of this app

deploy:
  1. requires a clean tree whose HEAD is on origin/main, runs the test suite;
  2. checks server headroom (memory, disk, load) before building on the shared host;
  3. streams `git archive <commit>` over SSH into /var/lib/icey-stats/releases/<commit>;
  4. builds icey-stats:<commit> there, with the OCI revision label, and verifies the label;
  5. over a short-lived SSH tunnel to Dokploy: keeps the ingest token already in the compose env
     (generates one on the first deploy), sets image, host and token, uploads deploy/compose.yml
     from that commit, deploys and waits for the status;
  6. checks the container (healthy, non-root, read-only, no published ports), then polls
     https://<host>/healthz until it answers 200;
  7. sets the Actions secret ICEY_STATS_INGEST_TOKEN on leonardovida/icey through `gh` (stdin only);
  8. prunes old release dirs and images of this app, keeping the newest three.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from vpslib import (  # noqa: E402
    APP,
    RELEASES,
    ROOT,
    SERVICE,
    TOKEN_NAME,
    Abort,
    Dokploy,
    Remote,
    assert_commit,
    git,
    info,
    ingest_token,
    load_target,
    parse_env_text,
    prune,
    read_personal_apps_secrets,
    render_env,
)

MIN_MEM_KIB = 1024 * 1024  # 1 GiB available
MIN_DISK_KIB = 3 * 1024 * 1024  # 3 GiB free under /var/lib


def require_releasable_head(skip_tests: bool) -> str:
    if git("status", "--porcelain").strip():
        raise Abort("commit or set aside local changes before a release")
    commit = assert_commit(git("rev-parse", "HEAD").strip())
    git("fetch", "--quiet", "origin", "main")
    if subprocess.run(["git", "merge-base", "--is-ancestor", commit, "origin/main"], cwd=ROOT).returncode != 0:
        raise Abort("HEAD is not on origin/main; push it first")
    if not skip_tests:
        info("Running the test suite")
        if subprocess.run(["uv", "run", "pytest", "-q"], cwd=ROOT).returncode != 0:
            raise Abort("tests failed")
    return commit


def check_headroom(remote: Remote) -> None:
    mem, disk, load, cpus = remote.run(
        "awk '/MemAvailable/ {print $2}' /proc/meminfo; df --output=avail -k /var/lib | tail -1; "
        "cut -d' ' -f1 /proc/loadavg; nproc"
    ).split()
    if int(mem) < MIN_MEM_KIB:
        raise Abort(f"server has only {int(mem) // 1024} MiB available memory; not building now")
    if int(disk) < MIN_DISK_KIB:
        raise Abort(f"server has only {int(disk) // 1024} MiB free under /var/lib; not building now")
    if float(load) > int(cpus):
        raise Abort(f"server load {load} exceeds {cpus} CPUs; not building now")
    info(f"Server headroom ok: {int(mem) // 1024} MiB memory, {int(disk) // 1024 // 1024} GiB disk, load {load}")


def build_release(remote: Remote, commit: str) -> str:
    image = f"{APP}:{commit}"
    path = f"{RELEASES}/{commit}"
    remote.run(f"install -d -m 0750 {RELEASES} {path}")
    archive = subprocess.Popen(["git", "archive", commit], cwd=ROOT, stdout=subprocess.PIPE)
    transfer = subprocess.run([*remote.ssh, f"tar -x -C {path}"], stdin=archive.stdout)
    archive.stdout.close()  # type: ignore[union-attr]
    if archive.wait() != 0 or transfer.returncode != 0:
        raise Abort("source transfer failed")
    info(f"Building {image} on the server")
    remote.run(
        f"cd {path} && if ! docker image inspect {image} >/dev/null 2>&1; then "
        f"docker build --build-arg RELEASE_COMMIT={commit} -t {image} .; fi",
        stream=True,
    )
    verify_image(remote, commit)
    return image


def verify_image(remote: Remote, commit: str) -> None:
    revision = remote.run(
        f"docker image inspect {APP}:{commit} --format '{{{{index .Config.Labels \"org.opencontainers.image.revision\"}}}}'",
        check=False,
    ).strip()
    if revision != commit:
        raise Abort(f"image {APP}:{commit[:12]} is missing or has the wrong revision label")


def wait_for_compose(api: Dokploy, compose_id: str) -> None:
    time.sleep(5)
    for _ in range(72):
        status = (api.get("compose.one", composeId=compose_id) or {}).get("composeStatus")
        if status == "done":
            return
        if status == "error":
            raise Abort("Dokploy reported a failed deployment; check its deployment log")
        time.sleep(5)
    raise Abort("timed out waiting for Dokploy")


def wait_for_container(remote: Remote, image: str) -> None:
    for _ in range(45):
        ids = remote.run(
            f"docker ps --filter ancestor={image} --filter label=com.docker.compose.service={SERVICE} --format '{{{{.ID}}}}'"
        ).split()
        if len(ids) == 1:
            inspect = json.loads(remote.run(f"docker inspect {ids[0]}"))[0]
            if (inspect["State"].get("Health") or {}).get("Status") == "healthy":
                host = inspect["HostConfig"]
                if any(host.get("PortBindings") or {}):
                    raise Abort("the container publishes a host port")
                if inspect["Config"].get("User") in ("", "0", "root", None):
                    raise Abort("the container runs as root")
                if not host.get("ReadonlyRootfs") or "ALL" not in (host.get("CapDrop") or []):
                    raise Abort("the container is missing the strict profile")
                info("Container healthy: non-root, read-only, no capabilities, no published ports")
                return
        time.sleep(4)
    raise Abort("the container did not become healthy; roll back with scripts/deploy.py rollback <commit>")


def wait_for_public_health(hostname: str) -> None:
    url = f"https://{hostname}/healthz"
    for attempt in range(45):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status == 200 and response.read().strip() == b"ok":
                    info(f"{url} answers 200")
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(4 if attempt < 30 else 10)  # the first deploy waits for the Let's Encrypt certificate
    raise Abort(f"{url} did not answer 200")


def set_github_secret(repository: str, token: str) -> None:
    # The value goes to gh on stdin only: never an argument, never printed.
    result = subprocess.run(
        ["gh", "secret", "set", TOKEN_NAME, "--repo", repository, "--app", "actions"],
        input=token, text=True, stdout=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise Abort(f"could not set the {TOKEN_NAME} secret on {repository}; rerun deploy once gh is authenticated")
    info(f"Set the {TOKEN_NAME} Actions secret on {repository}")


def release(commit: str, *, title: str, set_secret: bool, target: dict[str, Any], remote: Remote, api_key: str) -> None:
    image = f"{APP}:{commit}"
    compose_file = git("show", f"{commit}:deploy/compose.yml")
    with Dokploy(remote.server, api_key) as api:
        current = api.get("compose.one", composeId=target["composeId"]) or {}
        if current.get("name") != target.get("projectName", APP) or current.get("sourceType") != "raw":
            raise Abort("the Dokploy compose is not the dedicated raw icey-stats stack")
        previous = parse_env_text(current.get("env") or "").get("ICEY_STATS_IMAGE")
        token, generated = ingest_token(current.get("env"))
        env = render_env({"ICEY_STATS_IMAGE": image, "ICEY_STATS_HOST": target["hostname"], TOKEN_NAME: token})
        api.post("compose.update", {"composeId": target["composeId"], "sourceType": "raw",
                                    "composeFile": compose_file, "env": env})
        if generated:
            info("Generated a new ingest token (first deploy); it is stored only in Dokploy and GitHub")
        api.post("compose.deploy", {"composeId": target["composeId"], "title": title[:100],
                                    "description": f"scripts/deploy.py {commit}"})
        info("Deployment queued; waiting for Dokploy")
        wait_for_compose(api, target["composeId"])
    wait_for_container(remote, image)
    wait_for_public_health(target["hostname"])
    if set_secret:
        set_github_secret(target.get("githubSecretRepository", "leonardovida/icey"), token)
    del token, env
    prune(remote, protect={commit} | ({previous.split(":", 1)[1]} if previous and ":" in previous else set()))
    if previous and previous != image:
        info(f"Previous image retained: {previous}. Roll back with: uv run scripts/deploy.py rollback {previous.split(':', 1)[1]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    deploy = sub.add_parser("deploy", help="build and deploy HEAD")
    deploy.add_argument("--skip-tests", action="store_true")
    deploy.add_argument("--skip-github-secret", action="store_true", help="do not set the Actions secret")
    rollback = sub.add_parser("rollback", help="redeploy a retained image")
    rollback.add_argument("commit")
    sub.add_parser("prune", help="keep the newest three release dirs and images")
    args = parser.parse_args()

    target = load_target()
    if args.action != "prune" and not target.get("composeId"):
        raise Abort("run scripts/provision.py and set composeId in deploy/target.json first")
    if args.action == "deploy":
        commit = require_releasable_head(args.skip_tests)
    secrets_ = read_personal_apps_secrets()
    remote = Remote(secrets_["server"] or target["server"])

    if args.action == "prune":
        running = remote.run(
            f"docker ps --filter label=com.docker.compose.service={SERVICE} --format '{{{{.Image}}}}'"
        ).split()
        prune(remote, protect={i.split(":", 1)[1] for i in running if i.startswith(f"{APP}:")})
        return
    if args.action == "deploy":
        check_headroom(remote)
        build_release(remote, commit)
        release(commit, title=f"deploy {commit[:12]}", set_secret=not args.skip_github_secret,
                target=target, remote=remote, api_key=secrets_["api_key"])
    else:
        commit = assert_commit(args.commit)
        git("cat-file", "-e", f"{commit}^{{commit}}")
        verify_image(remote, commit)
        info(f"Rolling back to retained image {APP}:{commit}")
        release(commit, title=f"rollback {commit[:12]}", set_secret=False,
                target=target, remote=remote, api_key=secrets_["api_key"])
    info(f"Released {commit} to https://{target['hostname']}")


if __name__ == "__main__":
    main()
