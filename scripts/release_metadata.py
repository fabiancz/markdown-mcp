"""Validate release inputs and keep prerelease images away from the stable alias."""

import os
import re
import tomllib
from pathlib import Path


def metadata(version: str, env: dict[str, str]) -> dict[str, str]:
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:rc[1-9]\d*)?", version):
        raise ValueError("Package version must be X.Y.Z or X.Y.ZrcN")
    tagged = env["GITHUB_REF_TYPE"] == "tag"
    release = tagged or env["GITHUB_EVENT_NAME"] == "workflow_dispatch"
    requested = (
        env["GITHUB_REF_NAME"].removeprefix("v")
        if tagged
        else env.get("REQUESTED_VERSION") or version
    )
    if release and requested != version:
        raise ValueError(f"Release version {requested!r} does not match package version {version}")
    return {
        "image": "ghcr.io/" + env["GITHUB_REPOSITORY"].lower(),
        "tag": version if release else "sha-" + env["GITHUB_SHA"][:12],
        "latest": str(release and "rc" not in version).lower(),
    }


def main():
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    values = metadata(version, dict(os.environ))
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
