"""Run review integration tests on a pinned, disposable loopback Forgejo instance."""

import argparse
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

DEFAULT_IMAGE = (
    "data.forgejo.org/forgejo/forgejo:15.0.9@sha256:"
    "91a5310c86934339e16bd06b6078aada836e3d8935b2d70f6598108cbfaed5d1"
)
ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--application-image", help="Also test the MCP image over HTTP")
    parser.add_argument("--platform", default="linux/arm64")
    args = parser.parse_args()
    name = "markdown-mcp-forgejo-test-" + uuid.uuid4().hex[:12]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://localhost:{port}"
    try:
        run(
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:3000",
            "-e",
            "FORGEJO__security__INSTALL_LOCK=true",
            "-e",
            "FORGEJO__database__DB_TYPE=sqlite3",
            "-e",
            f"FORGEJO__server__ROOT_URL={url}/",
            "-e",
            "FORGEJO__server__DISABLE_SSH=true",
            "-e",
            "FORGEJO__repository__DEFAULT_BRANCH=main",
            "-e",
            "FORGEJO__service__DISABLE_REGISTRATION=true",
            args.image,
        )
        with httpx.Client(trust_env=False, timeout=1) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    response = client.get(url + "/api/v1/version")
                    if response.is_success:
                        print("Fixture version: " + response.json()["version"], flush=True)
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Disposable Forgejo did not start")
                time.sleep(0.5)
        # A regular account with ownership of synthetic repositories; no admin bypass.
        run(
            "docker",
            "exec",
            "--user",
            "git",
            name,
            "forgejo",
            "admin",
            "user",
            "create",
            "--username",
            "fixture",
            "--password",
            "synthetic-fixture-password-29",
            "--email",
            "fixture@example.invalid",
            "--must-change-password=false",
        )
        env = dict(
            os.environ,
            MCP_TEST_FORGEJO_URL=url,
            MCP_TEST_APPLICATION_IMAGE=args.application_image or "",
            MCP_TEST_APPLICATION_PLATFORM=args.platform,
        )
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-s", "tests/test_forgejo_live.py"],
            cwd=ROOT,
            env=env,
            check=True,
        )
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)


if __name__ == "__main__":
    main()
