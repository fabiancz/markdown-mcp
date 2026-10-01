import asyncio
import hashlib
import subprocess

import pytest
from conftest import git
from fastmcp import Client

from obsidian_mcp.config import Settings
from obsidian_mcp.git_source import ManagedGit
from obsidian_mcp.models import DomainError, Principal
from obsidian_mcp.read import Reader
from obsidian_mcp.server import create_server
from obsidian_mcp.write import Writer

ACTOR = Principal(subject="tunnel_operator", roles=frozenset({"reader", "writer"}))


class FixtureForge:
    def __init__(self, workspace):
        self.workspace = workspace
        self.prs = []
        self.fail_create = False
        self.calls = 0
        self.evidence = {"version": "synthetic", "repository_id": 1}

    async def verify(self):
        return self.evidence

    async def create_or_find_cr(self, change):
        self.calls += 1
        if not self.prs:
            self.prs.append(
                {
                    "number": 1,
                    "html_url": "https://fixture.invalid/owner/vault/pulls/1",
                    "head": {"sha": change["head_commit"]},
                    "state": "open",
                    "merged": False,
                    "merge_commit_sha": None,
                }
            )
        if self.fail_create:
            self.fail_create = False
            raise DomainError("PROVIDER_UNAVAILABLE", "Injected unknown POST outcome", True)
        return self.prs[0]

    async def find(self, change):
        return self.prs[0] if self.prs else None

    async def get_cr(self, number):
        return self.prs[number - 1]

    def validate(self, pr, change):
        return pr


@pytest.fixture
def writes(vault):
    settings, _, origin, files = vault
    remote = origin.parent / "vault.git"
    subprocess.run(
        ["git", "clone", "--bare", str(origin), str(remote)], check=True, capture_output=True
    )
    settings = Settings(**dict(settings.model_dump(), write_enabled=True))

    class LocalGit(ManagedGit):
        def run(self, args, **kwargs):
            if args[:2] == ["remote", "get-url"]:
                args = ["config", "--get", "remote.origin.url"]
            return super().run(
                ["-c", f"url.{remote}.insteadOf={settings.git_repo_url}", *args], **kwargs
            )

    reader = Reader(settings, LocalGit(settings))
    writer = Writer(settings, reader)
    adapter = FixtureForge(writer.workspace)
    writer.adapter = adapter
    return settings, reader, writer, remote, origin, files


async def draft(writer, path="notes/New.md", content="# New\n", key="test", operations=None):
    snapshot = await writer.reader.snapshot()
    return (
        await writer.prepare_change(
            operations or [{"op": "create", "path": path, "content": content}],
            "Update notes",
            key,
            snapshot.snapshot_id,
            actor=ACTOR,
        )
    )["data"]


async def queue(writer, prepared):
    return await writer.submit_change(
        prepared["change_id"], prepared["diff_hash"], wait_seconds=0, actor=ACTOR
    )


def retry_now(writer, id):
    change = writer.store.get(id)
    change["next_attempt"] = 0
    writer.store.save(change, "test_retry")


async def test_atomic_operations_isolation_backlinks_noop_and_owner(writes):
    settings, reader, writer, remote, origin, files = writes
    before = await reader.snapshot()
    source = reader.source
    (source.checkout / "notes/Backup.md").write_text("external unstaged edit")
    (source.checkout / "foreign.md").write_text("external staged content")
    git(source.checkout, "add", "foreign.md")
    revision = hashlib.sha256(files["notes/Backup.md"].encode()).hexdigest()
    operations = [
        {
            "op": "rename",
            "path": "notes/Backup.md",
            "destination": "notes/Renamed.md",
            "expected_revision": revision,
        },
        {
            "op": "replace",
            "path": "notes/Body.md",
            "content": "# Updated body\n",
            "expected_revision": hashlib.sha256(files["notes/Body.md"].encode()).hexdigest(),
        },
        {
            "op": "delete",
            "path": "notes/Empty.md",
            "expected_revision": hashlib.sha256(b"").hexdigest(),
        },
        {"op": "create", "path": "notes/New.md", "content": "# New note\n"},
    ]
    prepared = await draft(writer, operations=operations)
    assert prepared["status"] == "prepared" and not prepared["accepted"]
    assert prepared["backlinks"] and "notes/Body.md" in {b["source"] for b in prepared["backlinks"]}
    submitted = await queue(writer, prepared)
    assert submitted["data"]["accepted"] and not submitted["data"]["cr_open"]
    await writer.run_once()
    change = writer.store.get(prepared["change_id"])
    assert change["status"] == "awaiting_review" and not change["visible_in_read"]
    head = change["head_commit"]
    tree = writer.workspace.tree(head)
    assert "notes/Renamed.md" in tree and "notes/Backup.md" not in tree
    assert "notes/Empty.md" not in tree and "foreign.md" not in tree
    assert git(source.checkout, "show", head + ":notes/Body.md") == b"# Updated body\n"
    assert (source.checkout / "notes/Backup.md").read_text() == "external unstaged edit"
    assert b"foreign.md" in git(source.checkout, "diff", "--cached", "--name-only")
    assert (
        writer.workspace.run(["ls-remote", "origin", "refs/heads/main"]).split()[0].decode()
        == before.source_commit
    )
    assert (await reader.read_note("notes/Backup.md"))["data"]["text"] == files["notes/Backup.md"]
    assert writer.workspace.remote_head(change["branch"]) == head
    assert (settings.repo_root / "worktrees" / change["change_id"]).is_dir()
    await queue(writer, prepared)
    assert len(writer.adapter.prs) == 1
    denied = Principal(subject="another", roles=frozenset({"reader", "writer"}))
    with pytest.raises(DomainError, match="No change"):
        await writer.get_change(change["change_id"], actor=denied)
    assert not (await writer.list_changes(actor=denied))["data"]["items"]
    noop = await draft(
        writer,
        key="noop",
        operations=[
            {
                "op": "replace",
                "path": "notes/Empty.md",
                "expected_revision": hashlib.sha256(b"").hexdigest(),
                "content": "",
            }
        ],
    )
    assert noop["status"] == "no_change" and noop["diff"] == ""
    await queue(writer, noop)
    await writer.run_once()
    assert len(writer.adapter.prs) == 1


async def test_replay_concurrency_restart_and_unknown_post(writes):
    settings, reader, writer, remote, origin, files = writes
    await reader.snapshot()
    first, second = await asyncio.gather(draft(writer), draft(writer))
    assert first["change_id"] == second["change_id"]
    with pytest.raises(DomainError) as error:
        await draft(writer, content="different")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    with pytest.raises(DomainError) as error:
        await writer.submit_change(first["change_id"], "wrong", actor=ACTOR)
    assert error.value.code == "DIFF_MISMATCH"
    await queue(writer, first)
    writer.adapter.fail_create = True
    await writer.run_once()
    failed = writer.store.get(first["change_id"])
    assert failed["status"] == "retry_wait" and failed["cr_number"] is None
    assert len(writer.adapter.prs) == 1 and failed["pushed"]
    restarted = Writer(settings, reader, writer.adapter)
    retry_now(restarted, first["change_id"])
    await restarted.run_once()
    actual = restarted.store.get(first["change_id"])
    assert actual["cr_number"] == 1 and actual["head_commit"] == failed["head_commit"]
    assert len(writer.adapter.prs) == 1
    repeat = await draft(restarted)
    assert repeat["change_id"] == first["change_id"]


@pytest.mark.parametrize("boundary", ["committed", "pushed", "cr_reconciled"])
async def test_process_crash_after_effect_reconciles_once(writes, monkeypatch, boundary):
    settings, reader, writer, remote, origin, files = writes
    prepared = await draft(writer)
    await queue(writer, prepared)
    save = writer.store.save

    def crash(change, event):
        if event == boundary:
            raise RuntimeError("Injected process death after effect")
        save(change, event)

    monkeypatch.setattr(writer.store, "save", crash)
    with pytest.raises(RuntimeError):
        await writer.run_once()
    restarted = Writer(settings, reader, writer.adapter)
    await restarted.run_once()
    actual = restarted.store.get(prepared["change_id"])
    assert actual["status"] == "awaiting_review" and len(writer.adapter.prs) == 1
    assert restarted.workspace.remote_head(actual["branch"]) == actual["head_commit"]
    commits = restarted.workspace.run(
        ["rev-list", "--count", actual["base_commit"] + ".." + actual["head_commit"]]
    )
    assert commits.strip() == b"1"


async def test_remote_collision_hooks_filters_and_changed_target(writes):
    settings, reader, writer, remote, origin, files = writes
    before = await reader.snapshot()
    marker = origin.parent / "must-not-execute"
    hook = reader.source.checkout / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    git(reader.source.checkout, "config", "filter.evil.clean", f"touch '{marker}'")
    git(reader.source.checkout, "config", "filter.evil.smudge", f"touch '{marker}'")
    git(reader.source.checkout, "config", "filter.evil.required", "true")
    (reader.source.checkout / ".gitattributes").write_text("*.md filter=evil\n")
    prepared = await draft(writer)
    (origin / "unrelated.md").write_text("concurrent target addition")
    git(origin, "add", "unrelated.md")
    git(origin, "commit", "-m", "test: concurrent target change")
    subprocess.run(
        ["git", "-C", str(origin), "push", str(remote), "main"], check=True, capture_output=True
    )
    await queue(writer, prepared)
    await writer.run_once()
    change = writer.store.get(prepared["change_id"])
    assert not marker.exists()
    assert change["base_commit"] == before.source_commit and change["warnings"]
    assert change["target_commit"] != before.source_commit
    assert "unrelated.md" not in writer.workspace.tree(change["head_commit"])
    assert "unrelated.md" in {
        n["path"] for n in (await reader.list_notes(limit=50))["data"]["items"]
    }
    collision = await draft(writer, path="notes/Collision.md", key="collision")
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(remote),
            "update-ref",
            "refs/heads/" + collision["branch"],
            before.source_commit,
        ],
        check=True,
    )
    await queue(writer, collision)
    await writer.run_once()
    blocked = writer.store.get(collision["change_id"])
    assert blocked["code"] == "BRANCH_CHANGED" and blocked["status"] == "needs_attention"
    assert writer.workspace.remote_head(collision["branch"]) == before.source_commit
    assert len(writer.adapter.prs) == 1


@pytest.mark.parametrize(
    "operation,code",
    [
        ({"op": "create", "path": "../Outside.md", "content": "x"}, "INVALID_PATH"),
        ({"op": "create", "path": ".obsidian/X.md", "content": "x"}, "INVALID_PATH"),
        ({"op": "create", "path": "notes/link.md", "content": "x"}, "PATH_COLLISION"),
        ({"op": "create", "path": "notes/Link.md/Nested.md", "content": "x"}, "INVALID_PATH"),
        (
            {"op": "replace", "path": "notes/Link.md", "expected_revision": "x", "content": "x"},
            "NOTE_NOT_FOUND",
        ),
        ({"op": "delete", "path": "notes/Empty.md", "expected_revision": "x"}, "REVISION_MISMATCH"),
        ({"op": "create", "path": "script.py", "content": "x"}, "INVALID_PATH"),
        ({"op": "create", "path": "New.md", "content": "---\nx: [\n---\n"}, "INVALID_CONTENT"),
        ({"op": "create", "path": "New.md", "content": "<<<<<<< ours\n"}, "INVALID_CONTENT"),
        ({"op": "create", "path": "New.md", "content": "x", "remote": "evil"}, "INVALID_OPERATION"),
    ],
)
async def test_invalid_transaction_has_no_branch_or_pr(writes, operation, code):
    settings, reader, writer, remote, origin, files = writes
    with pytest.raises(DomainError) as error:
        await draft(writer, operations=[operation])
    assert error.value.code == code
    assert not writer.store.all() and not writer.adapter.prs
    assert not writer.workspace.run(["for-each-ref", "refs/heads/mcp/"])


async def test_policy_cancel_pagination_and_background_mcp(writes):
    settings, reader, writer, remote, origin, files = writes
    drafts = [await draft(writer, path=f"notes/N{i}.md", key=f"key{i}") for i in range(3)]
    first = (await writer.list_changes(limit=1, actor=ACTOR))["data"]
    second = (await writer.list_changes(limit=1, cursor=first["next_cursor"], actor=ACTOR))["data"]
    assert first["items"][0]["change_id"] != second["items"][0]["change_id"]
    with pytest.raises(DomainError):
        await writer.list_changes(limit=2, cursor=first["next_cursor"], actor=ACTOR)
    await writer.cancel_change(drafts[0]["change_id"], actor=ACTOR)
    with pytest.raises(DomainError):
        await queue(writer, drafts[0])
    await queue(writer, drafts[1])
    settings.write_enabled = False
    await writer.run_once()
    assert writer.store.get(drafts[1]["change_id"])["status"] == "blocked_policy"
    settings.write_enabled = True
    server = create_server(settings, reader)
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert not tools["prepare_change"].annotations.readOnlyHint
        assert not tools["prepare_change"].annotations.openWorldHint
        assert tools["submit_change"].annotations.openWorldHint
        assert tools["get_change"].annotations.readOnlyHint
        bad = await client.call_tool(
            "submit_change",
            {
                "change_id": drafts[2]["change_id"],
                "expected_diff_hash": drafts[2]["diff_hash"],
                "mode": "yolo",
            },
            raise_on_error=False,
        )
        assert bad.is_error and "PERMISSION_DENIED" in bad.content[0].text
        result = await client.call_tool("get_change", {"change_id": drafts[2]["change_id"]})
        assert result.data["data"]["status"] == "prepared"
    restarted = Writer(settings, reader, writer.adapter)
    await restarted.start()
    try:
        result = await restarted.submit_change(
            drafts[2]["change_id"], drafts[2]["diff_hash"], wait_seconds=5, actor=ACTOR
        )
        assert result["data"]["status"] == "awaiting_review"
    finally:
        await restarted.stop()


@pytest.mark.parametrize("boundary", ["pushed", "cr_reconciled"])
async def test_revocation_and_cancel_reconcile_unknown_effects(writes, monkeypatch, boundary):
    settings, reader, writer, remote, origin, files = writes
    prepared = await draft(writer)
    await queue(writer, prepared)
    save = writer.store.save

    def crash(change, event):
        if event == boundary:
            raise RuntimeError("Injected unknown external effect")
        save(change, event)

    monkeypatch.setattr(writer.store, "save", crash)
    with pytest.raises(RuntimeError):
        await writer.run_once()
    recovery = Writer(settings, reader, writer.adapter)
    # Cancellation is durable even though the remote result has not yet been recorded.
    await recovery.cancel_change(prepared["change_id"], actor=ACTOR)
    settings.write_enabled = False
    await recovery.run_once()
    change = recovery.store.get(prepared["change_id"])
    assert change["status"] == "cancelled" and change["pushed"]
    assert writer.workspace.remote_head(change["branch"]) == change["head_commit"]
    assert len(writer.adapter.prs) == (1 if boundary == "cr_reconciled" else 0)
    if boundary == "cr_reconciled":
        assert change["cr_number"] == 1 and change["cr_url"]


async def test_merged_index_retry_preserves_truth_without_republishing(writes, monkeypatch):
    settings, reader, writer, remote, origin, files = writes
    prepared = await draft(writer)
    await queue(writer, prepared)
    await writer.run_once()
    change = writer.store.get(prepared["change_id"])
    # A fixture reviewer advances the bare target, outside product write code.
    subprocess.run(
        ["git", "--git-dir", str(remote), "update-ref", "refs/heads/main", change["head_commit"]],
        check=True,
    )
    writer.adapter.prs[0].update(
        merged=True, state="closed", merge_commit_sha=change["head_commit"]
    )
    retry_now(writer, prepared["change_id"])
    sync = reader._synchronize

    def unavailable():
        raise DomainError("SYNC_UNAVAILABLE", "Injected index failure", True)

    monkeypatch.setattr(reader, "_synchronize", unavailable)
    await writer.run_once()
    pending = writer.store.get(prepared["change_id"])
    assert pending["merged"] and not pending["visible_in_read"] and pending["status"] == "merged"
    calls = writer.adapter.calls
    monkeypatch.setattr(reader, "_synchronize", sync)
    retry_now(writer, prepared["change_id"])
    await writer.run_once()
    result = writer.store.get(prepared["change_id"])
    assert result["status"] == "indexed" and result["visible_in_read"]
    assert writer.adapter.calls == calls and len(writer.adapter.prs) == 1
    assert (await reader.read_note("notes/New.md"))["data"]["text"] == "# New\n"


async def test_foreign_worktree_edit_after_commit_crash_is_preserved(writes, monkeypatch):
    settings, reader, writer, remote, origin, files = writes
    prepared = await draft(writer)
    await queue(writer, prepared)
    save = writer.store.save

    def crash(change, event):
        if event == "committed":
            raise RuntimeError("Injected process death")
        save(change, event)

    monkeypatch.setattr(writer.store, "save", crash)
    with pytest.raises(RuntimeError):
        await writer.run_once()
    file = settings.repo_root / "worktrees" / prepared["change_id"] / "notes/New.md"
    file.write_text("foreign worktree edit")
    recovery = Writer(settings, reader, writer.adapter)
    await recovery.run_once()
    assert recovery.store.get(prepared["change_id"])["code"] == "BRANCH_CHANGED"
    assert file.read_text() == "foreign worktree edit"
    assert not recovery.workspace.remote_head(prepared["branch"])


async def test_concurrent_cancellation_during_post_is_not_lost(writes):
    settings, reader, writer, remote, origin, files = writes
    prepared = await draft(writer)
    await queue(writer, prepared)
    create = writer.adapter.create_or_find_cr

    async def cancel_during_create(change):
        result = await create(change)
        # Exercise the same CAS path as a simultaneous HTTP cancellation request.
        cancellation = writer.store.get(change["change_id"])
        cancellation["cancel_requested"] = True
        writer.store.save(cancellation, "concurrent_cancel")
        return result

    writer.adapter.create_or_find_cr = cancel_during_create
    await writer.run_once()
    await writer.run_once()
    change = writer.store.get(prepared["change_id"])
    assert (
        change["cancel_requested"] and change["status"] == "cancelled" and change["cr_number"] == 1
    )
    assert len(writer.adapter.prs) == 1
