#!/usr/bin/env python3
"""One-time creation of the Dokploy project "icey-stats" and its raw Compose stack.

    uv run scripts/provision.py

Prints only the new composeId on stdout (progress goes to stderr). Put it in deploy/target.json,
commit, then run scripts/deploy.py deploy. Nothing is deployed here.

Refuses to run when deploy/target.json already has a composeId, or when Dokploy already has a project
or a compose named "icey-stats". It only creates: it never updates or deletes other projects.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vpslib import (  # noqa: E402
    APP,
    DOKPLOY_ID,
    ROOT,
    Abort,
    Dokploy,
    compose_names,
    first_id,
    info,
    load_target,
    project_names,
    read_personal_apps_secrets,
    walk,
)


def main() -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    target = load_target()
    name = target.get("projectName", APP)
    if target.get("composeId"):
        raise Abort("deploy/target.json already has a composeId; this stack is provisioned")
    compose_file = (ROOT / "deploy" / "compose.yml").read_text()
    secrets_ = read_personal_apps_secrets()

    with Dokploy(secrets_["server"] or target["server"], secrets_["api_key"]) as api:
        projects = api.get("project.all")
        if not isinstance(projects, list):
            raise Abort("unexpected project.all response; not creating anything")
        if name in project_names(projects):
            raise Abort(f'a Dokploy project named "{name}" already exists')
        if name in compose_names(projects) or name in compose_names(api.get("compose.search", name=name)):
            raise Abort(f'a Dokploy compose named "{name}" already exists')

        created = api.post("project.create", {
            "name": name,
            "description": "Anonymous usage statistics for the Icey macOS app (github.com/leonardovida/icey-stats)",
        })
        project_id = first_id(created, "projectId", nested="project")
        if not project_id or not DOKPLOY_ID.match(project_id):
            raise Abort("project.create did not return a projectId")
        info(f"Created project {name}")

        environment_id = first_id(created, "environmentId", nested="environment")
        if not environment_id:
            environments = api.get("environment.byProjectId", projectId=project_id) or []
            chosen = [e for e in environments if isinstance(e, dict) and e.get("name") == "production"] or environments
            environment_id = first_id(chosen[0], "environmentId") if chosen else None
        if not environment_id or not DOKPLOY_ID.match(environment_id):
            raise Abort("could not find the new project's environment; finish in the Dokploy panel")

        compose = api.post("compose.create", {
            "name": name,
            "description": "icey-stats service (raw Compose, image built on the server by scripts/deploy.py)",
            "environmentId": environment_id,
            "composeType": "docker-compose",
            "appName": name,
            "sourceType": "raw",
            "composeFile": compose_file,
        })
        compose_id = first_id(compose, "composeId")
        if not compose_id:
            found = [c for c in walk(api.get("compose.search", name=name, environmentId=environment_id))
                     if c.get("composeId") and c.get("name") == name]
            compose_id = first_id(found[0], "composeId") if len(found) == 1 else None
        if not compose_id or not DOKPLOY_ID.match(compose_id):
            raise Abort("compose.create did not return a composeId; look it up in the Dokploy panel")
        # compose.create may ignore sourceType on some versions; make the raw source explicit.
        api.post("compose.update", {"composeId": compose_id, "sourceType": "raw", "composeFile": compose_file})
        info("Created the raw Compose stack. Set composeId in deploy/target.json, commit, then deploy.")
    print(compose_id)


if __name__ == "__main__":
    main()
