"""Read review context and append durable, approved updates to an existing PR."""

import asyncio
import hashlib
import json
import re
import time
import uuid
from datetime import UTC, datetime

from .models import DomainError


class ReviewWorkflow:
    def review_parent(self, change_id, actor):
        change = self.store.get(change_id, actor.subject)
        if change.get("parent_change_id") or not change.get("cr_number"):
            raise DomainError("CHANGE_STATE", "Use the original change ID of a published PR")
        return change

    async def review_pr(self, change):
        pr = await self.adapter.get_cr(change["cr_number"])
        self.adapter.validate(pr, change)
        if not re.fullmatch(r"[0-9a-f]{40}", pr["head"]["sha"]):
            raise DomainError("PROVIDER_REJECTED", "Invalid PR head SHA")
        return pr

    @staticmethod
    def open_pr(pr):
        if pr["merged"] or pr["state"] != "open":
            raise DomainError("CHANGE_STATE", "Only an open, unmerged PR can be updated")

    async def get_change_review(self, change_id, limit=20, cursor=None, *, actor=None):
        actor = self.actor(actor)
        self.reader.policy.authorize(actor, "read")
        self.reader.validate_limit(limit)
        change = await asyncio.to_thread(self.review_parent, change_id, actor)
        try:
            async with asyncio.timeout(25):
                pr = await self.review_pr(change)
                items = await self.adapter.review_comments(change["cr_number"])
                after = await self.review_pr(change)
        except TimeoutError:
            raise DomainError(
                "PROVIDER_UNAVAILABLE", "Review retrieval timed out; retry", True
            ) from None
        if pr["head"]["sha"] != after["head"]["sha"]:
            raise DomainError("HEAD_CHANGED", "PR changed while reading review; retry", True)
        items = [
            item
            for item in items
            if not item.get("path") or self.reader.policy.permits(item["path"])
        ]
        identity = [actor.subject, change_id, pr["head"]["sha"], limit, items]
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        offset = 0
        if cursor:
            try:
                _, offset = self.reader.decode_cursor(cursor, fingerprint, change_id)
            except DomainError:
                raise DomainError(
                    "REVIEW_CHANGED", "Review or cursor changed; restart pagination"
                ) from None
        data = {
            "change_id": change_id,
            "cr_url": change["cr_url"],
            "head_sha": pr["head"]["sha"],
            "state": after["state"],
            "merged": after["merged"],
            "review_revision": fingerprint,
            "items": [],
            "next_cursor": None,
            "content_is_untrusted": True,
        }
        snapshot = self.reader.index.snapshot()
        for item in items[offset : offset + limit]:
            candidate = dict(data, items=data["items"] + [item])
            next_offset = offset + len(candidate["items"])
            candidate["next_cursor"] = (
                self.reader.cursor(change_id, next_offset, fingerprint)
                if next_offset < len(items)
                else None
            )
            try:
                self.reader.envelope(candidate, snapshot)
            except DomainError:
                if not data["items"]:
                    raise
                break
            data = candidate
        return self.reader.envelope(data, snapshot)

    async def fetch_review_head(self, change, expected):
        if not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise DomainError("INVALID_OPERATION", "Use the exact 40-character PR head SHA")
        pr = await self.review_pr(change)
        if pr["head"]["sha"] != expected:
            raise DomainError("HEAD_CHANGED", "PR head changed; read the current review")

        def fetch():
            with self.source.lock():
                self.source.initialize()
                if self.workspace.fetch_branch(change["branch"]) != expected:
                    raise DomainError("HEAD_CHANGED", "PR branch changed; read the current review")

        await asyncio.to_thread(fetch)
        return pr

    async def read_change_note(
        self, change_id, path, head_sha, start_line=1, end_line=None, *, actor=None
    ):
        actor = self.actor(actor)
        self.reader.policy.authorize(actor, "read")
        change = await asyncio.to_thread(self.review_parent, change_id, actor)
        if (
            type(start_line) is not int
            or start_line < 1
            or (end_line is not None and (type(end_line) is not int or end_line < start_line))
        ):
            raise DomainError("INVALID_RANGE", "Use a valid 1-based line range")
        await self.fetch_review_head(change, head_sha)

        def read():
            with self.source.lock():
                tree = self.workspace.tree(head_sha)
                self.workspace.path(path, tree, existing=True)
                size = int(self.workspace.run(["cat-file", "-s", tree[path][1]]))
                if size > self.settings.max_file_bytes:
                    raise DomainError("RESPONSE_LIMIT", "PR note exceeds the file limit")
                raw = self.workspace.run(["cat-file", "blob", tree[path][1]])
                try:
                    text = raw.decode("utf-8")
                except UnicodeError:
                    raise DomainError("INVALID_CONTENT", "PR note is not UTF-8") from None
                return raw, text

        raw, text = await asyncio.to_thread(read)
        lines = text.splitlines(keepends=True)
        if start_line > max(1, len(lines)):
            raise DomainError("INVALID_RANGE", "Start line exceeds the note length")
        last = min(end_line or len(lines), len(lines))
        selected, size = [], 0
        for line in lines[start_line - 1 : last]:
            cost = len(json.dumps(line, ensure_ascii=False).encode())
            if size + cost > max(0, self.settings.max_response_bytes - 4096):
                break
            selected.append(line)
            size += cost
        if last >= start_line and not selected:
            raise DomainError("RESPONSE_LIMIT", "A single line exceeds the response limit")
        actual = start_line + len(selected) - 1
        return self.reader.envelope(
            {
                "change_id": change_id,
                "path": path,
                "head_sha": head_sha,
                "source": "pr_head",
                "revision": hashlib.sha256(raw).hexdigest(),
                "text": "".join(selected),
                "line_start": start_line,
                "line_end": actual,
                "line_count": len(lines),
                "truncated": actual < last,
                "next_start_line": actual + 1 if actual < len(lines) else None,
            },
            self.reader.index.snapshot(),
        )

    async def prepare_change_update(
        self, change_id, operations, summary, expected_head_sha, idempotency_key, *, actor=None
    ):
        actor = self.actor(actor)
        self.authorize(actor)
        normalized, summary, request_hash = self.normalize_request(
            operations, summary, idempotency_key, ["update", change_id, expected_head_sha]
        )
        prior = await asyncio.to_thread(self.store.find, actor.subject, idempotency_key)
        if prior:
            if prior["request_hash"] != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Key already belongs to another request")
            return self.envelope(prior, diff=True)
        parent = await asyncio.to_thread(self.review_parent, change_id, actor)
        if parent["cancel_requested"]:
            raise DomainError("CHANGE_STATE", "The original change was cancelled")
        pr = await self.fetch_review_head(parent, expected_head_sha)
        self.open_pr(pr)

        def prepare():
            with self.source.lock():
                plan = self.workspace.prepare(expected_head_sha, normalized)
                id = "upd_" + uuid.uuid4().hex
                update = dict(parent, **plan)
                update.update(
                    change_id=id,
                    parent_change_id=change_id,
                    version=0,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    base_commit=expected_head_sha,
                    summary=summary,
                    policy_hash=self.settings.policy_hash,
                    status="prepared" if plan["diff"] else "no_change",
                    submitted=False,
                    created_at=time.time(),
                    updated_at=time.time(),
                    next_attempt=0,
                    attempts=0,
                    commit_date=datetime.now(UTC).isoformat(),
                    commit_name=self.settings.git_commit_name,
                    commit_email=self.settings.git_commit_email,
                    head_commit=None,
                    cr_state=pr["state"],
                    target_commit=None,
                    merge_commit=None,
                    merged_by=None,
                    provider_head_commit=expected_head_sha,
                    push_intent=False,
                    pushed=False,
                    cr_intent=False,
                    merged=False,
                    visible_in_read=False,
                    cancel_requested=False,
                    code=None,
                    message=None,
                    warnings=[],
                    backlinks=[],
                )
                self.envelope(update, diff=True)
                result = self.store.reserve(update)
                self.workspace.run(
                    ["update-ref", "refs/mcp-drafts/" + result["change_id"], expected_head_sha]
                )
                return result

        update = await asyncio.to_thread(prepare)
        return self.envelope(update, diff=True)

    async def submit_change_update(
        self, update_id, expected_diff_hash, wait_seconds=15, *, actor=None
    ):
        actor = self.actor(actor)
        self.authorize(actor)
        update = await asyncio.to_thread(self.store.get, update_id, actor.subject)
        if not update.get("parent_change_id"):
            raise DomainError("CHANGE_STATE", "Use an update ID from prepare_change_update")
        parent = await asyncio.to_thread(self.review_parent, update["parent_change_id"], actor)
        if not update["submitted"] and update["status"] != "no_change":
            if parent["cancel_requested"]:
                raise DomainError("CHANGE_STATE", "The original change was cancelled")
            pr = await self.review_pr(parent)
            self.open_pr(pr)
            if pr["head"]["sha"] != update["base_commit"]:
                raise DomainError("HEAD_CHANGED", "PR head changed; prepare a new update")
        return await self.submit_change(
            update_id, expected_diff_hash, wait_seconds=wait_seconds, actor=actor
        )

    def _step_update(self, update):
        parent = self.store.get(update["parent_change_id"], update["owner"])
        # Resolve an unknown push before cancellation, policy, PR closure or head checks.
        if update["push_intent"]:
            with self.source.lock():
                self.source.initialize()
                remote = self.workspace.remote_head(update["branch"])
                if remote:
                    actual = self.workspace.fetch_branch(update["branch"])
                    history = self.workspace.run(["rev-list", actual]).decode().splitlines()
                    if update["head_commit"] in history:
                        update.update(
                            status="updated",
                            pushed=True,
                            provider_head_commit=actual,
                            code=None,
                            message=None,
                        )
                        self.store.save(update, "update_push_reconciled")
                        return
                if remote != update["base_commit"]:
                    raise DomainError("HEAD_CHANGED", "PR branch changed; update was not replayed")
        if update["cancel_requested"] or parent["cancel_requested"]:
            update["status"] = "cancelled"
            update["push_intent"] = False
            self.store.save(update, "update_cancelled")
            return
        if not self.owner_allowed(update["owner"]):
            raise DomainError("PERMISSION_DENIED", "Initiator can no longer update the PR")
        if (
            update["policy_hash"] != self.settings.policy_hash
            or update["target_branch"] != self.source.branch
        ):
            raise DomainError("POLICY_CHANGED", "Policy or target changed; prepare a new update")
        pr = asyncio.run(self.review_pr(parent))
        self.open_pr(pr)
        if pr["head"]["sha"] != update["base_commit"]:
            raise DomainError("HEAD_CHANGED", "PR head changed; prepare a new update")
        with self.source.lock():
            if self.workspace.fetch_branch(update["branch"]) != update["base_commit"]:
                raise DomainError("HEAD_CHANGED", "PR branch changed; prepare a new update")
            if not update["head_commit"]:
                update["status"] = "validating"
                self.store.save(update, "update_commit_intent")
                update["head_commit"] = self.workspace.commit(update)
                update["status"] = "committed"
                self.store.save(update, "update_committed")
            # Persist authorization before push; restart observes it without repeating effects.
            current = self.store.get(update["change_id"])
            parent_now = self.store.get(parent["change_id"])
            if current.get("version", 0) != update.get("version", 0):
                raise DomainError("CHANGE_STATE", "Update changed concurrently", True)
            if (
                current["cancel_requested"]
                or parent_now["cancel_requested"]
                or not self.owner_allowed(update["owner"])
            ):
                raise DomainError("PERMISSION_DENIED", "Update publication is no longer authorized")
            latest_pr = asyncio.run(self.review_pr(parent_now))
            self.open_pr(latest_pr)
            if latest_pr["head"]["sha"] != update["base_commit"]:
                raise DomainError("HEAD_CHANGED", "PR head changed; prepare a new update")
            update["push_intent"] = True
            self.store.save(update, "update_push_intent")
            self.workspace.publish_update(update)
            update.update(
                status="updated",
                pushed=True,
                provider_head_commit=update["head_commit"],
                code=None,
                message=None,
            )
            self.store.save(update, "update_pushed")
