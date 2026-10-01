"""Opt-in tests for a disposable loopback Forgejo instance, never a production vault."""

import base64
import json
import os
import uuid
from urllib.parse import urlsplit

import httpx
import pytest

from obsidian_mcp.config import Settings
from obsidian_mcp.forgejo import ForgejoAdapter
from obsidian_mcp.models import DomainError
from obsidian_mcp.read import Reader
from obsidian_mcp.write import Writer

pytestmark = pytest.mark.forgejo_live


@pytest.fixture
def live_settings(tmp_path):
    url = os.getenv("MCP_TEST_FORGEJO_URL")
    if not url:
        pytest.skip("Set MCP_TEST_FORGEJO_URL to a disposable loopback Forgejo instance")
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
        pytest.fail("Live fixture requires loopback; production endpoints are not accepted")
    user = "fixture"
    password = "synthetic-fixture-password-29"
    repo = "mcp-test-" + uuid.uuid4().hex[:12]
    token_name = repo + "-token"
    with httpx.Client(base_url=url, trust_env=False, timeout=20) as client:
        created = client.post(
            f"/api/v1/users/{user}/tokens",
            auth=(user, password),
            json={"name": token_name, "scopes": ["write:repository"]},
        )
        created.raise_for_status()
        token = created.json()
        client.headers["Authorization"] = "token " + token["sha1"]
        response = client.post(
            "/api/v1/user/repos",
            auth=(user, password),
            json={"name": repo, "private": True, "auto_init": True, "default_branch": "main"},
        )
        response.raise_for_status()
        try:
            yield Settings(
                deployment_mode="tunnel",
                git_provider="forgejo",
                git_repo_url=f"{url}/{user}/{repo}.git",
                git_username=user,
                git_pat=token["sha1"],
                git_allow_http=url.startswith("http:"),
                repo_root=tmp_path / "repo",
                data_root=tmp_path / "data",
                write_enabled=True,
                write_poll_seconds=0.1,
            )
        finally:
            client.delete(f"/api/v1/repos/{user}/{repo}", auth=(user, password)).raise_for_status()
            client.delete(
                f"/api/v1/users/{user}/tokens/{token['id']}", auth=(user, password)
            ).raise_for_status()


async def test_live_review_unknown_post_restart_manual_update_merge_and_index(live_settings):
    settings = live_settings
    reader = Reader(settings)
    base = await reader.snapshot()

    class TimeoutAfterCreate(ForgejoAdapter):
        inject = True

        async def request(self, method, path, **kwargs):
            response = await super().request(method, path, **kwargs)
            if method == "POST" and path.endswith("/pulls") and self.inject:
                self.inject = False
                raise DomainError(
                    "PROVIDER_UNAVAILABLE", "Injected timeout after real creation", True
                )
            return response

    adapter = TimeoutAfterCreate(settings)
    writer = Writer(settings, reader, adapter)
    prepared = (
        await writer.prepare_change(
            [
                {
                    "op": "create",
                    "path": "notes/Live.md",
                    "content": "# Live review\noriginal intent\n",
                },
                {"op": "create", "path": "notes/Second.md", "content": "# Atomic second file\n"},
            ],
            "Add synthetic review notes",
            "live-unknown-post",
            base.snapshot_id,
        )
    )["data"]
    await writer.submit_change(prepared["change_id"], prepared["diff_hash"], wait_seconds=0)
    await writer.run_once()
    unknown = writer.store.get(prepared["change_id"])
    assert unknown["status"] == "retry_wait" and unknown["cr_number"] is None
    old_head = unknown["head_commit"]
    unknown["next_attempt"] = 0
    writer.store.save(unknown, "test_retry")
    restarted_reader = Reader(settings)
    restarted = Writer(settings, restarted_reader)
    await restarted.run_once()
    current = restarted.store.get(prepared["change_id"])
    assert current["status"] == "awaiting_review" and current["head_commit"] == old_head
    assert not current["merged"] and not current["visible_in_read"]
    assert (await restarted_reader.list_notes())["meta"]["source_commit"] == base.source_commit
    actual = await restarted.adapter.find(current)
    assert actual["number"] == current["cr_number"]
    # A reviewer edits the existing PR and adds a real commit to its source branch.
    await restarted.adapter.request(
        "PATCH",
        restarted.adapter.path + f"/pulls/{current['cr_number']}",
        json={"title": "Reviewed synthetic notes"},
    )
    content = await restarted.adapter.request(
        "GET", restarted.adapter.path + "/contents/notes/Live.md", params={"ref": current["branch"]}
    )
    await restarted.adapter.request(
        "PUT",
        restarted.adapter.path + "/contents/notes/Live.md",
        json={
            "branch": current["branch"],
            "sha": content["sha"],
            "message": "test: reviewer edit",
            "content": base64.b64encode(b"# Live review\nreviewer improvement\n").decode(),
        },
    )
    current["next_attempt"] = 0
    restarted.store.save(current, "test_poll")
    await restarted.run_once()
    observed = restarted.store.get(current["change_id"])
    assert observed["provider_head_commit"] != old_head
    assert restarted.workspace.remote_head(current["branch"]) == observed["provider_head_commit"]
    # This merge is a test reviewer action, never performed by the product adapter.
    async with httpx.AsyncClient(trust_env=False) as reviewer:
        response = await reviewer.post(
            settings.api_url + restarted.adapter.path + f"/pulls/{current['cr_number']}/merge",
            headers={"Authorization": "token " + settings.git_pat.get_secret_value()},
            json={
                "Do": "squash",
                "head_commit_id": observed["provider_head_commit"],
                "force_merge": False,
                "delete_branch_after_merge": False,
            },
        )
        response.raise_for_status()
    observed["next_attempt"] = 0
    restarted.store.save(observed, "test_poll")
    await restarted.run_once()
    result = (await restarted.get_change(current["change_id"]))["data"]
    assert result["merged"] and result["visible_in_read"] and result["status"] == "indexed"
    assert (await restarted_reader.read_note("notes/Live.md"))["data"][
        "text"
    ] == "# Live review\nreviewer improvement\n"
    assert (await restarted_reader.read_note("notes/Second.md"))["data"][
        "text"
    ] == "# Atomic second file\n"
    assert result["forge_evidence"]["version"].startswith("15.0.9")
    evidence = {k: result[k] for k in ["status", "merged", "visible_in_read", "forge_evidence"]}
    print("Live Forgejo evidence: " + json.dumps(evidence))


async def test_live_manual_close_cancellation_and_wrong_pat(live_settings):
    settings = live_settings
    reader = Reader(settings)
    base = await reader.snapshot()
    writer = Writer(settings, reader)
    prepared = (
        await writer.prepare_change(
            [{"op": "create", "path": "Closed.md", "content": "closed\n"}],
            "Synthetic close",
            "live-close",
            base.snapshot_id,
        )
    )["data"]
    await writer.submit_change(prepared["change_id"], prepared["diff_hash"], wait_seconds=0)
    await writer.run_once()
    current = writer.store.get(prepared["change_id"])
    assert current["status"] == "awaiting_review"
    cancelled = (await writer.cancel_change(prepared["change_id"]))["data"]
    assert cancelled["status"] == "cancelled" and cancelled["cr_open"]
    await writer.adapter.request(
        "PATCH", writer.adapter.path + f"/pulls/{current['cr_number']}", json={"state": "closed"}
    )
    current = writer.store.get(prepared["change_id"])
    current["next_attempt"] = 0
    writer.store.save(current, "test_poll")
    await writer.run_once()
    result = (await writer.get_change(prepared["change_id"]))["data"]
    assert result["status"] == "closed" and not result["merged"] and not result["visible_in_read"]
    assert (await writer.adapter.create_or_find_cr(writer.store.get(prepared["change_id"])))[
        "number"
    ] == current["cr_number"]
    wrong = Settings(**dict(settings.model_dump(), git_pat="synthetic-invalid-token"))
    with pytest.raises(DomainError) as error:
        await ForgejoAdapter(wrong).verify()
    assert error.value.code == "PERMISSION_DENIED"


async def test_live_container_http_worker_recreate_and_review(live_settings, tmp_path):
    import asyncio
    import subprocess
    import time

    from fastmcp import Client

    image = os.getenv("MCP_TEST_APPLICATION_IMAGE")
    if not image:
        pytest.skip("Set MCP_TEST_APPLICATION_IMAGE to test the built MCP container")
    settings = live_settings
    name = "mcp-write-image-test-" + uuid.uuid4().hex[:12]
    platform = os.getenv("MCP_TEST_APPLICATION_PLATFORM", "linux/arm64")
    root = tmp_path / "deployment"
    root.mkdir()
    for path in [root / "repo", root / "data"]:
        path.mkdir(mode=0o777)
        path.chmod(0o777)
    env_path = root / "fixture.env"
    values = {
        "DEPLOYMENT_MODE": "tunnel",
        "GIT_PROVIDER": "forgejo",
        "GIT_REPO_URL": settings.git_repo_url.replace("localhost", "host.docker.internal"),
        "GIT_ALLOW_HTTP": "true",
        "GIT_USERNAME": settings.git_username,
        "GIT_PAT": settings.git_pat.get_secret_value(),
        "WRITE_ENABLED": "true",
        "WRITE_POLL_SECONDS": "0.2",
    }
    env_path.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    env_path.chmod(0o600)
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        "--platform",
        platform,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-p",
        "127.0.0.1::8000",
        "--env-file",
        str(env_path),
        "--mount",
        f"type=bind,source={root / 'repo'},target=/repo",
        "--mount",
        f"type=bind,source={root / 'data'},target=/data",
    ]
    if __import__("sys").platform.startswith("linux"):
        command += ["--add-host", "host.docker.internal:host-gateway"]
    command.append(image)

    def docker(*args):
        return (
            subprocess.check_output(["docker", *args], stderr=subprocess.DEVNULL).decode().strip()
        )

    async def ready_url():
        port = json.loads(docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", name))[
            "8000/tcp"
        ][0]["HostPort"]
        url = f"http://localhost:{port}"
        deadline = time.monotonic() + 60
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                try:
                    if (await client.get(url + "/health/ready")).is_success:
                        return url + "/mcp"
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        pytest.fail("MCP container did not become ready")

    try:
        subprocess.run(command, capture_output=True, check=True)
        async with Client(await ready_url()) as client:
            base = (await client.call_tool("list_notes")).data["meta"]["snapshot_id"]
            prepared = (
                await client.call_tool(
                    "prepare_change",
                    {
                        "operations": [
                            {"op": "create", "path": "Container.md", "content": "# Image write\n"}
                        ],
                        "summary": "Container review",
                        "idempotency_key": "container-restart",
                        "base_snapshot_id": base,
                    },
                )
            ).data["data"]
            submitted = (
                await client.call_tool(
                    "submit_change",
                    {
                        "change_id": prepared["change_id"],
                        "expected_diff_hash": prepared["diff_hash"],
                        "wait_seconds": 20,
                    },
                )
            ).data["data"]
            assert submitted["status"] == "awaiting_review" and submitted["cr_url"]
        docker("rm", "-f", name)
        subprocess.run(command, capture_output=True, check=True)
        async with Client(await ready_url()) as client:
            result = (
                await client.call_tool("get_change", {"change_id": prepared["change_id"]})
            ).data["data"]
            assert result["cr_number"] == submitted["cr_number"] and not result["visible_in_read"]
            repeated = (
                await client.call_tool(
                    "prepare_change",
                    {
                        "operations": [
                            {"op": "create", "path": "Container.md", "content": "# Image write\n"}
                        ],
                        "summary": "Container review",
                        "idempotency_key": "container-restart",
                        "base_snapshot_id": base,
                    },
                )
            ).data["data"]
            assert repeated["change_id"] == prepared["change_id"]
            bad = await client.call_tool(
                "read_note", {"path": "Container.md"}, raise_on_error=False
            )
            assert bad.is_error
        adapter = ForgejoAdapter(settings)
        await adapter.verify()
        prs = await adapter.request("GET", adapter.path + "/pulls", params={"state": "all"})
        assert len(prs) == 1
        print(json.dumps({"platform": platform, "http_prepare_submit_recreate": "passed"}))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        # Restore ownership of only the temporary mounts on Linux.
        if __import__("sys").platform.startswith("linux"):
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0:0",
                    "--mount",
                    f"type=bind,source={root},target=/fixture",
                    "--entrypoint",
                    "chown",
                    image,
                    "-hR",
                    f"{os.getuid()}:{os.getgid()}",
                    "/fixture",
                ],
                check=True,
                capture_output=True,
            )
