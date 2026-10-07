"""Opt-in tests for a disposable loopback Forgejo instance, never a production vault."""

import asyncio
import base64
import json
import os
import time
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


async def reviewer_merge(client, pr_url, head, **kwargs):
    # Forgejo recomputes mergeability asynchronously after a Git push.
    # Only fixture reviewer actions use this helper; the product never merges.
    deadline = time.monotonic() + 20
    while True:
        response = await client.get(pr_url, **kwargs)
        response.raise_for_status()
        pr = response.json()
        assert pr["head"]["sha"] == head and not pr["merged"]
        if pr.get("mergeable"):
            response = await client.post(
                pr_url + "/merge",
                json={
                    "Do": "squash",
                    "head_commit_id": head,
                    "force_merge": False,
                    "delete_branch_after_merge": False,
                },
                **kwargs,
            )
            if response.status_code != 405:
                response.raise_for_status()
                return
        if time.monotonic() >= deadline:
            pytest.fail("Synthetic PR did not become mergeable within 20 seconds")
        await asyncio.sleep(0.2)


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
        response = client.post(
            "/api/v1/user/repos",
            auth=(user, password),
            json={"name": repo, "private": True, "auto_init": True, "default_branch": "main"},
        )
        response.raise_for_status()
        created = client.post(
            f"/api/v1/users/{user}/tokens",
            auth=(user, password),
            json={
                "name": token_name,
                "scopes": ["write:repository", "read:issue"],
                "repositories": [{"owner": user, "name": repo}],
            },
        )
        created.raise_for_status()
        token = created.json()
        assert token["repositories"]
        client.headers["Authorization"] = "token " + token["sha1"]
        response = client.put(
            f"/api/v1/repos/{user}/{repo}/collaborators/reviewer",
            auth=(user, password),
            json={"permission": "write"},
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
        await reviewer_merge(
            reviewer,
            settings.api_url + restarted.adapter.path + f"/pulls/{current['cr_number']}",
            observed["provider_head_commit"],
            headers={"Authorization": "token " + settings.git_pat.get_secret_value()},
        )
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


async def test_live_review_comments_stale_update_unknown_push_and_merge(live_settings):
    settings = live_settings
    reader = Reader(settings)
    writer = Writer(settings, reader)
    base = await reader.snapshot()
    draft = (
        await writer.prepare_change(
            [{"op": "create", "path": "Review.md", "content": "# Review\nOriginal line\n"}],
            "Review follow-up fixture",
            "review-followup",
            base.snapshot_id,
        )
    )["data"]
    await writer.submit_change(draft["change_id"], draft["diff_hash"], wait_seconds=0)
    await writer.run_once()
    parent = writer.store.get(draft["change_id"])
    auth = ("reviewer", "synthetic-reviewer-password-29")
    async with httpx.AsyncClient(trust_env=False, timeout=20, auth=auth) as reviewer:
        repo_url = settings.api_url + writer.adapter.path
        r = await reviewer.post(
            repo_url + f"/issues/{parent['cr_number']}/comments",
            json={"body": "Please address this feedback"},
        )
        r.raise_for_status()
        r = await reviewer.post(
            repo_url + f"/pulls/{parent['cr_number']}/reviews",
            json={
                "event": "COMMENT",
                "body": "Review summary",
                "commit_id": parent["head_commit"],
                "comments": [
                    {"path": "Review.md", "body": "Improve the second line", "new_position": 2}
                ],
            },
        )
        r.raise_for_status()
        review = (await writer.get_change_review(parent["change_id"]))["data"]
        assert {item["kind"] for item in review["items"]} == {"discussion", "review", "inline"}
        assert all(item["author"]["login"] == "reviewer" for item in review["items"])
        inline = next(item for item in review["items"] if item["kind"] == "inline")
        assert inline["path"] == "Review.md" and inline["body"] == "Improve the second line"
        assert inline["line"] == 2 and inline["commit_sha"] == parent["head_commit"]
        assert inline["original_commit_sha"] == ""  # Provider leaves it empty on a new comment.
        assert inline["diff_hunk"]
        note = (
            await writer.read_change_note(parent["change_id"], "Review.md", review["head_sha"])
        )["data"]

        async def prepare(key, head):
            return (
                await writer.prepare_change_update(
                    parent["change_id"],
                    [
                        {
                            "op": "replace",
                            "path": "Review.md",
                            "expected_revision": note["revision"],
                            "content": "# Review\nAddressed feedback\n",
                        }
                    ],
                    "Address feedback",
                    head,
                    key,
                )
            )["data"]

        stale = await prepare("stale-review-update", review["head_sha"])
        r = await reviewer.post(
            repo_url + "/contents/Reviewer.md",
            json={
                "branch": parent["branch"],
                "message": "test: manual reviewer commit",
                "content": base64.b64encode(b"# Keep reviewer addition\n").decode(),
            },
        )
        r.raise_for_status()
        with pytest.raises(DomainError) as error:
            await writer.submit_change_update(
                stale["update_id"], stale["diff_hash"], wait_seconds=0
            )
        assert error.value.code == "HEAD_CHANGED"
        current = (await writer.get_change_review(parent["change_id"]))["data"]
        update = await prepare("current-review-update", current["head_sha"])
        publish = writer.workspace.publish_update

        def unknown(change):
            publish(change)
            raise DomainError(
                "PROVIDER_UNAVAILABLE", "Injected timeout after real update push", True
            )

        writer.workspace.publish_update = unknown
        await writer.submit_change_update(update["update_id"], update["diff_hash"], wait_seconds=0)
        await writer.run_once()
        pending = writer.store.get(update["update_id"])
        assert pending["status"] == "retry_wait"
        pending["next_attempt"] = 0
        writer.store.save(pending, "test_retry")
        restarted = Writer(settings, Reader(settings))
        await restarted.run_once()
        result = restarted.store.get(update["update_id"])
        assert result["status"] == "updated"
        assert (
            restarted.workspace.run(["rev-parse", result["head_commit"] + "^"]).decode().strip()
            == current["head_sha"]
        )
        assert (
            restarted.workspace.run(["show", result["head_commit"] + ":Reviewer.md"])
            == b"# Keep reviewer addition\n"
        )
        assert (
            len(
                await restarted.adapter.request(
                    "GET", restarted.adapter.path + "/pulls", params={"state": "all"}
                )
            )
            == 1
        )
        await reviewer_merge(
            reviewer, repo_url + f"/pulls/{parent['cr_number']}", result["head_commit"]
        )
        parent_now = restarted.store.get(parent["change_id"])
        parent_now["next_attempt"] = 0
        restarted.store.save(parent_now, "test_poll")
        await restarted.run_once()
        assert restarted.store.get(parent["change_id"])["visible_in_read"]
        assert (await restarted.reader.read_note("Review.md"))["data"][
            "text"
        ] == "# Review\nAddressed feedback\n"
        print(
            "Live discussion/review/inline, stale-head, manual-commit, "
            "unknown-push and merge/index: passed"
        )


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
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert len(tools) == 17
            assert tools["upload_attachment"].meta["openai/fileParams"] == ["file"]
            status = (await client.call_tool("get_vault_status")).data["data"]
            assert status["limits"]["upload_file_bytes"] == 2000000
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
            review = (
                await client.call_tool("get_change_review", {"change_id": prepared["change_id"]})
            ).data["data"]
            note = (
                await client.call_tool(
                    "read_change_note",
                    {
                        "change_id": prepared["change_id"],
                        "path": "Container.md",
                        "head_sha": review["head_sha"],
                    },
                )
            ).data["data"]
            update = (
                await client.call_tool(
                    "prepare_change_update",
                    {
                        "change_id": prepared["change_id"],
                        "expected_head_sha": review["head_sha"],
                        "operations": [
                            {
                                "op": "replace",
                                "path": "Container.md",
                                "expected_revision": note["revision"],
                                "content": "# Image write\nReview follow-up\n",
                            }
                        ],
                        "summary": "Container follow-up",
                        "idempotency_key": "container-update-restart",
                    },
                )
            ).data["data"]
            applied = (
                await client.call_tool(
                    "submit_change_update",
                    {
                        "update_id": update["update_id"],
                        "expected_diff_hash": update["diff_hash"],
                        "wait_seconds": 20,
                    },
                )
            ).data["data"]
            assert applied["applied"] and applied["cr_number"] == submitted["cr_number"]
        docker("rm", "-f", name)
        subprocess.run(command, capture_output=True, check=True)
        async with Client(await ready_url()) as client:
            restored = (
                await client.call_tool("get_change", {"change_id": update["update_id"]})
            ).data["data"]
            assert restored["applied"] and restored["head_commit"] == applied["head_commit"]
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


async def test_live_attachment_review_restart_merge_and_exact_bytes(live_settings, monkeypatch):
    import hashlib

    from obsidian_mcp.uploads import ChatFile

    settings = live_settings
    reader = Reader(settings)
    base = await reader.snapshot()
    writer = Writer(settings, reader)
    raw = bytes(range(256)) * 7812 + bytes(range(128))
    # Download/TLS is covered separately; this live test exercises actual Git/Forgejo effects.
    monkeypatch.setattr("obsidian_mcp.uploads.download_file", lambda *args: raw)
    upload = (
        await writer.upload_attachment(
            ChatFile(
                download_url="https://files.oaiusercontent.com/fixture", file_id="fixture-file"
            ),
            "attachment",
        )
    )["data"]
    prepared = (
        await writer.prepare_change(
            [
                {
                    "op": "create_attachment",
                    "path": "attachments/data.bin",
                    "upload_id": upload["upload_id"],
                },
                {
                    "op": "create",
                    "path": "Attachment.md",
                    "content": "# Attachment\n[Download](attachments/data.bin)\n",
                },
            ],
            "Add attachment and note",
            "attachment-change",
            base.snapshot_id,
        )
    )["data"]
    assert len(prepared["diff"]) < 2000
    await writer.submit_change(prepared["change_id"], prepared["diff_hash"], wait_seconds=0)
    await writer.run_once()
    current = writer.store.get(prepared["change_id"])
    assert current["status"] == "awaiting_review", current
    head = current["head_commit"]
    with writer.source.lock():
        assert writer.workspace.run(["show", head + ":attachments/data.bin"]) == raw
    update = (
        await writer.prepare_change_update(
            prepared["change_id"],
            [
                {
                    "op": "create_attachment",
                    "path": "attachments/second.bin",
                    "upload_id": upload["upload_id"],
                }
            ],
            "Add second attachment to the same PR",
            head,
            "attachment-update",
        )
    )["data"]
    await writer.submit_change_update(update["update_id"], update["diff_hash"], wait_seconds=0)
    await writer.run_once()
    applied = writer.store.get(update["update_id"])
    assert applied["status"] == "updated", applied
    assert applied["cr_number"] == current["cr_number"]
    head = applied["head_commit"]
    current = writer.store.get(prepared["change_id"])
    # Expire transient staging before recreating the worker and observing a real squash merge.
    with writer.store.connect() as db:
        db.execute("UPDATE uploads SET expires=0")
    writer.uploads.cleanup()
    restarted = Writer(settings, Reader(settings))
    async with httpx.AsyncClient(trust_env=False, timeout=20) as client:
        await reviewer_merge(
            client,
            settings.api_url + writer.adapter.path + f"/pulls/{current['cr_number']}",
            head,
            auth=("reviewer", "synthetic-reviewer-password-29"),
        )
    current["next_attempt"] = 0
    writer.store.save(current, "test_poll")
    await restarted.run_once()
    result = restarted.store.get(prepared["change_id"])
    assert result["status"] == "indexed" and result["attachments_verified"] is True, result
    snapshot = await restarted.reader.snapshot()
    with restarted.source.lock():
        actual = restarted.workspace.run(["show", snapshot.source_commit + ":attachments/data.bin"])
        assert (
            restarted.workspace.run(["show", snapshot.source_commit + ":attachments/second.bin"])
            == raw
        )
    assert hashlib.sha256(actual).hexdigest() == upload["sha256"]
    assert (
        "[Download](attachments/data.bin)"
        in (await restarted.reader.read_note("Attachment.md"))["data"]["text"]
    )
