import hashlib

import pytest
from conftest import git
from test_write import ACTOR, draft, queue, retry_now
from test_write import writes as writes

from obsidian_mcp.models import DomainError, Principal
from obsidian_mcp.write import Writer


@pytest.fixture
async def reviewed(writes):
    settings, reader, writer, remote, origin, files = writes
    prepared = await draft(writer, content="# Draft\nfirst version\n")
    await queue(writer, prepared)
    await writer.run_once()
    parent = writer.store.get(prepared["change_id"])
    original_get = writer.adapter.get_cr

    async def current_pr(number):
        pr = dict(await original_get(number))
        pr["head"] = {"sha": writer.workspace.remote_head(parent["branch"])}
        return pr

    async def comments(number):
        return [
            {"kind": "discussion", "id": 1, "body": "Please improve this note"},
            {"kind": "inline", "id": 2, "body": "Replace this line", "path": "notes/New.md"},
        ]

    writer.adapter.get_cr = current_pr
    writer.adapter.review_comments = comments
    return settings, reader, writer, remote, origin, parent


async def update(writer, parent, key="update", content="# Draft\nimproved\n"):
    head = (await writer.get_change_review(parent["change_id"]))["data"]["head_sha"]
    note = (await writer.read_change_note(parent["change_id"], "notes/New.md", head))["data"]
    args = dict(
        change_id=parent["change_id"],
        operations=[
            {
                "op": "replace",
                "path": "notes/New.md",
                "expected_revision": note["revision"],
                "content": content,
            }
        ],
        summary="Address reviewer feedback",
        expected_head_sha=head,
        idempotency_key=key,
    )
    return (await writer.prepare_change_update(**args))["data"], args


async def test_review_reads_and_same_pr_update_idempotency(reviewed):
    settings, reader, writer, remote, origin, parent = reviewed
    first = (await writer.get_change_review(parent["change_id"], limit=1))["data"]
    second = (
        await writer.get_change_review(parent["change_id"], limit=1, cursor=first["next_cursor"])
    )["data"]
    assert first["items"][0]["kind"] == "discussion"
    assert second["items"][0]["kind"] == "inline" and not second["next_cursor"]
    with pytest.raises(DomainError):
        await reader.read_note("notes/New.md")
    note = (
        await writer.read_change_note(
            parent["change_id"], "notes/New.md", first["head_sha"], start_line=2
        )
    )["data"]
    assert note["text"] == "first version\n" and note["source"] == "pr_head"
    prepared, args = await update(writer, parent)
    assert (await writer.prepare_change_update(**args))["data"]["update_id"] == prepared[
        "update_id"
    ]
    with pytest.raises(DomainError) as error:
        await writer.submit_change_update(prepared["update_id"], "wrong", wait_seconds=0)
    assert error.value.code == "DIFF_MISMATCH"
    await writer.submit_change_update(prepared["update_id"], prepared["diff_hash"], wait_seconds=0)
    await writer.run_once()
    result = (await writer.get_change(prepared["update_id"]))["data"]
    assert result["applied"] and result["cr_number"] == parent["cr_number"]
    assert (
        writer.workspace.run(["rev-parse", result["head_commit"] + "^"]).decode().strip()
        == parent["head_commit"]
    )
    assert writer.store.get(parent["change_id"])["diff"] == parent["diff"]
    assert len(writer.adapter.prs) == 1
    assert (await writer.prepare_change_update(**args))["data"]["update_id"] == prepared[
        "update_id"
    ]
    await writer.submit_change_update(prepared["update_id"], prepared["diff_hash"], wait_seconds=0)
    assert (await writer.cancel_change(prepared["update_id"]))["data"]["applied"]
    second_update, _ = await update(
        writer, parent, key="second", content="# Draft\nsecond update\n"
    )
    await writer.submit_change_update(
        second_update["update_id"], second_update["diff_hash"], wait_seconds=0
    )
    await writer.run_once()
    second_result = writer.store.get(second_update["update_id"])
    assert second_result["status"] == "updated"
    assert (
        writer.workspace.run(["rev-parse", second_result["head_commit"] + "^"]).decode().strip()
        == result["head_commit"]
    )
    assert writer.store.get(parent["change_id"])["diff"] == parent["diff"]
    assert len(writer.adapter.prs) == 1
    with pytest.raises(DomainError):
        await reader.read_note("notes/New.md")


async def test_noop_update_and_idempotency_conflict(reviewed):
    settings, reader, writer, remote, origin, parent = reviewed
    prepared, args = await update(writer, parent, content="# Draft\nfirst version\n")
    assert prepared["status"] == "no_change"
    submitted = (await writer.submit_change_update(prepared["update_id"], prepared["diff_hash"]))[
        "data"
    ]
    assert not submitted["applied"] and not submitted["accepted"]
    args["summary"] = "Different intent"
    with pytest.raises(DomainError) as error:
        await writer.prepare_change_update(**args)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert writer.workspace.remote_head(parent["branch"]) == parent["head_commit"]


def reviewer_commit(writer, parent, origin):
    git(origin, "fetch", str(writer.source.checkout), parent["head_commit"])
    git(origin, "checkout", "--detach", "FETCH_HEAD")
    (origin / "Reviewer.md").write_text("# Preserve this manual addition\n")
    git(origin, "add", "Reviewer.md")
    git(origin, "commit", "-m", "test: reviewer addition")
    head = git(origin, "rev-parse", "HEAD").decode().strip()
    git(
        origin,
        "push",
        str(writer.source.root.parent / "vault.git"),
        "HEAD:refs/heads/" + parent["branch"],
    )
    return head


async def test_stale_head_and_preservation_of_manual_commits(reviewed):
    settings, reader, writer, remote, origin, parent = reviewed
    prepared, args = await update(writer, parent)
    manual = reviewer_commit(writer, parent, origin)
    with pytest.raises(DomainError) as error:
        await writer.submit_change_update(
            prepared["update_id"], prepared["diff_hash"], wait_seconds=0
        )
    assert error.value.code == "HEAD_CHANGED"
    fresh, _ = await update(writer, parent, key="fresh")
    await writer.submit_change_update(fresh["update_id"], fresh["diff_hash"], wait_seconds=0)
    await writer.run_once()
    result = writer.store.get(fresh["update_id"])
    assert result["status"] == "updated"
    assert (
        writer.workspace.run(["rev-parse", result["head_commit"] + "^"]).decode().strip() == manual
    )
    assert (
        writer.workspace.run(["show", result["head_commit"] + ":Reviewer.md"])
        == b"# Preserve this manual addition\n"
    )
    with pytest.raises(DomainError) as error:
        await writer.read_change_note(parent["change_id"], "notes/New.md", parent["head_commit"])
    assert error.value.code == "HEAD_CHANGED"


async def test_unknown_push_restart_reconciles_after_revocation_and_cancel(reviewed):
    settings, reader, writer, remote, origin, parent = reviewed
    prepared, _ = await update(writer, parent)
    publish = writer.workspace.publish_update

    def timeout(change):
        publish(change)
        raise DomainError("PROVIDER_UNAVAILABLE", "Injected unknown push outcome", True)

    writer.workspace.publish_update = timeout
    await writer.submit_change_update(prepared["update_id"], prepared["diff_hash"], wait_seconds=0)
    await writer.run_once()
    assert writer.store.get(prepared["update_id"])["status"] == "retry_wait"
    await writer.cancel_change(prepared["update_id"])
    retry_now(writer, prepared["update_id"])
    settings.write_enabled = False
    restarted = Writer(settings, reader, writer.adapter)
    await restarted.run_once()
    result = restarted.store.get(prepared["update_id"])
    assert result["status"] == "updated" and result["cancel_requested"]
    head = restarted.workspace.remote_head(parent["branch"])
    assert head == result["head_commit"]
    await restarted.run_once()
    assert restarted.workspace.remote_head(parent["branch"]) == head


async def test_parallel_updates_and_closed_pr_never_overwrite(reviewed):
    settings, reader, writer, remote, origin, parent = reviewed
    one, _ = await update(writer, parent, key="one")
    two, _ = await update(writer, parent, key="two", content="# Another result\n")
    await writer.submit_change_update(one["update_id"], one["diff_hash"], wait_seconds=0)
    await writer.submit_change_update(two["update_id"], two["diff_hash"], wait_seconds=0)
    await writer.run_once()
    outcomes = [writer.store.get(x["update_id"])["status"] for x in [one, two]]
    assert sorted(outcomes) == ["needs_attention", "updated"]
    writer.adapter.prs[0]["state"] = "closed"
    with pytest.raises(DomainError) as error:
        await update(writer, parent, key="closed")
    assert error.value.code == "CHANGE_STATE"


@pytest.mark.parametrize("stop", ["closed", "merged", "cancelled"])
async def test_queued_update_stops_after_pr_closure_merge_or_parent_cancel(reviewed, stop):
    settings, reader, writer, remote, origin, parent = reviewed
    prepared, _ = await update(writer, parent)
    await writer.submit_change_update(prepared["update_id"], prepared["diff_hash"], wait_seconds=0)
    if stop == "cancelled":
        await writer.cancel_change(parent["change_id"])
    else:
        writer.adapter.prs[0]["state"] = "closed"
        if stop == "merged":
            writer.adapter.prs[0].update(merged=True, merge_commit_sha=parent["base_commit"])
    await writer.run_once()
    record = writer.store.get(prepared["update_id"])
    assert record["status"] == ("cancelled" if stop == "cancelled" else "needs_attention")
    assert not record["push_intent"]
    assert writer.workspace.remote_head(parent["branch"]) == parent["head_commit"]


async def test_push_lease_rejects_manual_commit_between_check_and_push(reviewed):
    settings, reader, writer, remote, origin, parent = reviewed
    prepared, _ = await update(writer, parent)
    run = writer.workspace.run
    manual = None

    def raced(args, **kwargs):
        nonlocal manual
        if args[0] == "push":
            manual = reviewer_commit(writer, parent, origin)
        return run(args, **kwargs)

    writer.workspace.run = raced
    await writer.submit_change_update(prepared["update_id"], prepared["diff_hash"], wait_seconds=0)
    await writer.run_once()
    assert writer.store.get(prepared["update_id"])["status"] == "retry_wait"
    writer.workspace.run = run
    retry_now(writer, prepared["update_id"])
    await writer.run_once()
    assert writer.store.get(prepared["update_id"])["code"] == "HEAD_CHANGED"
    assert writer.workspace.remote_head(parent["branch"]) == manual


async def test_review_identity_cursor_policy_and_preview_budget(reviewed):
    settings, reader, writer, remote, origin, parent = reviewed
    other = Principal(subject="other", roles=ACTOR.roles)
    with pytest.raises(DomainError) as error:
        await writer.get_change_review(parent["change_id"], actor=other)
    assert error.value.code == "CHANGE_NOT_FOUND"
    page = (await writer.get_change_review(parent["change_id"], limit=1))["data"]

    async def changed(number):
        return [{"kind": "discussion", "id": 1, "body": "Edited comment"}]

    writer.adapter.review_comments = changed
    with pytest.raises(DomainError) as error:
        await writer.get_change_review(parent["change_id"], limit=1, cursor=page["next_cursor"])
    assert error.value.code == "REVIEW_CHANGED"
    with pytest.raises(DomainError):
        await writer.read_change_note(parent["change_id"], "../secret.md", page["head_sha"])
    prepared, args = await update(writer, parent)
    args.update(
        idempotency_key="large",
        operations=[
            {
                "op": "replace",
                "path": "notes/New.md",
                "expected_revision": hashlib.sha256(b"# Draft\nfirst version\n").hexdigest(),
                "content": "x" * 40000,
            }
        ],
    )
    with pytest.raises(DomainError) as error:
        await writer.prepare_change_update(**args)
    assert error.value.code == "RESPONSE_LIMIT" and not writer.store.find(ACTOR.subject, "large")
