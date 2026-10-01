"""Build-independent Compose smoke test against a disposable HTTP(S) Git remote."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml
from git_fixture import https_git

ROOT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args, result.stdout, result.stderr)
    return result.stdout


def restore_fixture_ownership(deployment: Path, image: str, platform: str):
    # Linux bind mounts retain UID 10001 ownership after the non-root service exits.
    # Restore only the disposable fixture mounts before TemporaryDirectory cleanup.
    run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            "--platform",
            platform,
            "--mount",
            f"type=bind,source={deployment / 'repo'},target=/repo",
            "--mount",
            f"type=bind,source={deployment / 'data'},target=/data",
            "--entrypoint",
            "chown",
            image,
            "-hR",
            f"{os.getuid()}:{os.getgid()}",
            "/repo",
            "/data",
        ]
    )


PROBE = """
import asyncio, json, urllib.request
from importlib.metadata import version
from fastmcp import Client
async def main():
    urllib.request.urlopen("http://127.0.0.1:8000/health/ready")
    async with Client("http://127.0.0.1:8000/mcp") as client:
        release_version = version("obsidian-read-mcp")
        assert client.initialize_result.serverInfo.version == release_version
        ping = (await client.call_tool("ping", {})).data["data"]
        assert ping["version"] == release_version
        assert ping["write_enabled"] is False
        assert ping["write_default_mode"] == "review"
        result = (await client.call_tool("search_notes", {"query":"zaloha"})).data
        hit = result["data"]["items"][0]
        note = (await client.call_tool("read_note", {
            "path":hit["path"], "snapshot_id":hit["snapshot_id"]})).data
        assert "záloha" in note["data"]["text"]
        assert note["data"]["revision"] == hit["revision"]
        print(json.dumps({"revision":hit["revision"],"snapshot":hit["snapshot_id"],"indexed_at":result["meta"]["indexed_at"]}))
asyncio.run(main())
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="ghcr.io/fabiancz/markdown-mcp:latest")
    parser.add_argument("--platform", default="linux/arm64")
    parser.add_argument("--git-http", action="store_true", help="Test opt-in HTTP Git transport")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="obsidian-container-") as directory:
        root = Path(directory).resolve()
        origin = root / "owner/vault.git"
        origin.mkdir(parents=True)
        run(["git", "-C", str(origin), "init", "-b", "main"])
        assert run(["git", "-C", str(origin), "rev-parse", "--show-toplevel"]).strip() == str(
            origin
        )
        run(["git", "-C", str(origin), "config", "user.name", "Fixture"])
        run(["git", "-C", str(origin), "config", "user.email", "fixture@example.invalid"])
        (origin / "Backup.md").write_text("# Backups\nzáloha\n")
        run(["git", "-C", str(origin), "add", "Backup.md"])
        run(["git", "-C", str(origin), "commit", "-m", "test: container fixture"])
        with https_git(root, tls=not args.git_http) as (port, cert, records):
            scheme = "http" if args.git_http else "https"
            for profile in ("tunnel", "oauth"):
                deployment = root / profile
                deployment.mkdir()
                for name in ("repo", "data"):
                    (deployment / name).mkdir(mode=0o777)
                    (deployment / name).chmod(0o777)
                shutil.copy(ROOT / f"examples/{profile}/docker-compose.yaml", deployment)
                values = {
                    "IMAGE": args.image,
                    "GIT_PROVIDER": "forgejo",
                    "GIT_REPO_URL": f"{scheme}://host.docker.internal:{port}/owner/vault.git",
                    "GIT_ALLOW_HTTP": str(args.git_http).lower(),
                    "GIT_USERNAME": "fixture",
                    "GIT_PAT": "test-only",
                    "TUNNEL_IMAGE": (
                        "ghcr.io/openai/tunnel-client@sha256:"
                        "119799b778ba8411a124f53588f9dc837fd62ba03e5fadf77d675123c92ab58e"
                    ),
                    "OPENAI_TUNNEL_ID": "synthetic-unused",
                    "OPENAI_TUNNEL_API_KEY": "synthetic-unused",
                    "PUBLIC_BASE_URL": "https://mcp.example.com",
                    "GITHUB_OAUTH_CLIENT_ID": "synthetic-client",
                    "GITHUB_OAUTH_CLIENT_SECRET": "synthetic-oauth-secret",
                    "GITHUB_ALLOWED_USER_IDS": "42",
                    "OAUTH_REDIRECT_URIS": "https://client.example/callback",
                    "HOST_PORT": "0",
                }
                (deployment / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()))
                override = {
                    "services": {
                        "mcp": {
                            "platform": args.platform,
                            "healthcheck": {"interval": "1s", "start_period": "2s"},
                        }
                    }
                }
                if cert:
                    override["services"]["mcp"].update(
                        environment={"GIT_CA_BUNDLE": "/fixture-ca.pem"},
                        volumes=[f"{cert}:/fixture-ca.pem:ro"],
                    )
                if sys.platform.startswith("linux"):
                    override["services"]["mcp"]["extra_hosts"] = [
                        "host.docker.internal:host-gateway"
                    ]
                (deployment / "override.yaml").write_text(yaml.safe_dump(override))
                compose = [
                    "docker",
                    "compose",
                    "--project-name",
                    f"obsidian-smoke-{os.getpid()}-{profile}",
                    "-f",
                    "docker-compose.yaml",
                    "-f",
                    "override.yaml",
                ]

                def command(*args, compose=compose, deployment=deployment, **kwargs):
                    return run([*compose, *args], cwd=deployment, **kwargs)

                try:
                    # Real workspace credentials are required to test the tunnel connection.
                    command("up", "-d", "mcp")
                    container = command("ps", "-q", "mcp").strip()
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        status = run(
                            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container]
                        ).strip()
                        if status == "healthy":
                            break
                        time.sleep(0.5)
                    assert status == "healthy", "Container did not become healthy"
                    if profile == "tunnel":
                        first = json.loads(command("exec", "-T", "mcp", "python", "-c", PROBE))
                        command("up", "-d", "--force-recreate", "mcp")
                        for _ in range(30):
                            try:
                                second = json.loads(
                                    command("exec", "-T", "mcp", "python", "-c", PROBE)
                                )
                                break
                            except subprocess.CalledProcessError:
                                time.sleep(0.5)
                        assert first == second
                        assert len(list((deployment / "repo").glob("checkout"))) == 1
                        assert (deployment / "data/index.sqlite").exists()
                        print(
                            json.dumps(
                                {
                                    "profile": profile,
                                    "platform": args.platform,
                                    "search_read_recreate": "passed",
                                    **first,
                                }
                            )
                        )
                    else:
                        probe = """
import urllib.request, urllib.error, json
url = 'http://127.0.0.1:8000/'
metadata = json.load(urllib.request.urlopen(url+'.well-known/oauth-authorization-server'))
assert 'S256' in metadata['code_challenge_methods_supported']
try:
    urllib.request.urlopen(urllib.request.Request(
        url+'mcp', data=b'{}', headers={'Content-Type':'application/json'}, method='POST'))
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError('Unauthenticated access allowed')
"""
                        try:
                            command("exec", "-T", "mcp", "python", "-c", probe)
                        except subprocess.CalledProcessError as error:
                            raise AssertionError(error.stderr) from None
                        print(
                            json.dumps(
                                {
                                    "profile": profile,
                                    "platform": args.platform,
                                    "discovery_and_401": "passed",
                                }
                            )
                        )
                    assert any(record["authorized"] for record in records)
                finally:
                    try:
                        command("down", "--remove-orphans")
                    finally:
                        if sys.platform.startswith("linux"):
                            restore_fixture_ownership(deployment, args.image, args.platform)


if __name__ == "__main__":
    main()
