"""Persistent review workflow. No automatic merge and no direct target push."""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import UTC, datetime

from .auth import principal
from .change_store import SQLiteChangeStore
from .config import Settings
from .forgejo import ForgejoAdapter
from .git_source import ManagedGit
from .git_workspace import DraftWorkspace, canonical_operations
from .models import ChangeStatus, DomainError, Principal
from .read import Reader
from .repo_lock import file_lock
from .review import ReviewWorkflow


class Writer(ReviewWorkflow):
    def __init__(self, settings: Settings, reader: Reader, adapter=None):
        self.settings, self.reader = settings, reader
        if not isinstance(reader.source, ManagedGit):
            raise DomainError("SOURCE_MISMATCH", "Write requires a managed Git source")
        self.source = reader.source
        self.workspace = DraftWorkspace(self.source)
        self.store = SQLiteChangeStore(settings)
        self.adapter = adapter or ForgejoAdapter(settings)
        self.wake = asyncio.Event()
        self.stopping = False
        self.task: asyncio.Task | None = None

    def authorize(self, actor: Principal):
        if not self.settings.write_enabled:
            raise DomainError("WRITE_DISABLED", "Write is disabled by server policy")
        self.reader.policy.authorize(actor, "write")
        if not self.owner_allowed(actor.subject):
            raise DomainError("PERMISSION_DENIED", "Identity is not in the current write policy")

    def owner_allowed(self, owner: str) -> bool:
        return self.settings.write_enabled and (
            (self.settings.deployment_mode == "tunnel" and owner == "tunnel_operator")
            or (
                self.settings.deployment_mode == "oauth"
                and owner.startswith("github:")
                and owner.removeprefix("github:") in self.settings.allowed_user_ids
                and owner.removeprefix("github:") in self.settings.write_user_ids
            )
        )

    def actor(self, actor: Principal | None) -> Principal:
        return actor or principal(self.settings)

    def envelope(self, change: dict, *, diff: bool = False):
        return self.reader.envelope(self.public(change, diff=diff), self.reader.index.snapshot())

    @staticmethod
    def public(change: dict, *, diff: bool = False) -> dict:
        fields = [
            "change_id",
            "status",
            "base_snapshot_id",
            "base_commit",
            "target_branch",
            "target_commit",
            "head_commit",
            "provider_head_commit",
            "branch",
            "summary",
            "diff_hash",
            "paths",
            "warnings",
            "backlinks",
            "cr_url",
            "cr_number",
            "merge_commit",
            "merged_by",
            "visible_in_read",
            "code",
            "message",
            "created_at",
            "updated_at",
            "forge_evidence",
            "cancel_requested",
            "parent_change_id",
        ]
        result = {k: change.get(k) for k in fields}
        result.update(
            accepted=change["submitted"],
            cr_open=change.get("cr_state") == "open",
            merged=change.get("merged", False),
            poll_after_seconds=10,
        )
        if diff:
            result["diff"] = change["diff"]
        if change.get("parent_change_id"):
            result["update_id"] = change["change_id"]
            result["applied"] = change["status"] == "updated"
        return result

    async def prepare_change(
        self,
        operations: list[dict],
        summary: str,
        idempotency_key: str,
        base_snapshot_id: str,
        *,
        actor: Principal | None = None,
    ) -> dict:
        actor = self.actor(actor)
        self.authorize(actor)
        normalized, summary, request_hash = self.normalize_request(
            operations, summary, idempotency_key, base_snapshot_id
        )
        existing = await asyncio.to_thread(self.store.find, actor.subject, idempotency_key)
        if existing:
            if existing["request_hash"] != request_hash:
                raise DomainError("IDEMPOTENCY_CONFLICT", "Key already belongs to another request")
            return self.envelope(existing, diff=True)
        base = await self.reader.snapshot(base_snapshot_id)
        change = await asyncio.to_thread(
            self._prepare, actor, normalized, summary, idempotency_key, request_hash, base
        )
        return self.envelope(change, diff=True)

    def normalize_request(self, operations, summary, idempotency_key, base_identity):
        normalized = canonical_operations(operations)
        if not 1 <= len(normalized) <= self.settings.write_max_operations:
            raise DomainError("INVALID_LIMIT", "Operation count exceeds the write limit")
        summary = summary.strip()
        if (
            not summary
            or len(summary.encode()) > 300
            or any(ord(c) < 32 or ord(c) == 127 for c in summary)
        ):
            raise DomainError("INVALID_OPERATION", "Summary must be a single bounded line")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
            raise DomainError("INVALID_OPERATION", "Use a 1-128 character ASCII idempotency key")
        payload = json.dumps([normalized, summary, base_identity], sort_keys=True)
        if len(payload.encode()) > self.settings.write_max_bytes:
            raise DomainError("RESPONSE_LIMIT", "Request exceeds the write byte limit")
        request_hash = hashlib.sha256(payload.encode()).hexdigest()
        return normalized, summary, request_hash

    def _prepare(self, actor, operations, summary, key, request_hash, base):
        with self.source.lock():
            self.source.initialize()
            plan = self.workspace.prepare(base.source_commit, operations)
            id = "chg_" + uuid.uuid4().hex
            backlinks = []
            with self.reader.index.connect() as db:
                self.reader.index.read_transaction(db, base.snapshot_id)
                for op in operations:
                    if op["op"] == "rename":
                        rows = db.execute(
                            "SELECT source,line FROM links WHERE snapshot=? AND target=?",
                            (base.snapshot_id, op["path"]),
                        ).fetchall()
                        backlinks.extend(
                            {"source": row[0], "line": row[1], "target": op["path"]} for row in rows
                        )
            change = dict(
                plan,
                change_id=id,
                version=0,
                owner=actor.subject,
                actor_id=hashlib.sha256(actor.subject.encode()).hexdigest()[:16],
                idempotency_key=key,
                request_hash=request_hash,
                repo_id=self.settings.source_identity,
                base_snapshot_id=base.snapshot_id,
                base_commit=base.source_commit,
                target_branch=self.source.branch,
                summary=summary,
                branch=self.workspace.branch(summary, id),
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
                warnings=[],
                backlinks=backlinks,
                head_commit=None,
                cr_url=None,
                cr_number=None,
                visible_in_read=False,
                merged=False,
                push_intent=False,
                pushed=False,
                cr_intent=False,
                cancel_requested=False,
            )
            # Validate the entire preview budget before reserving an idempotency key.
            self.envelope(change, diff=True)
            result = self.store.reserve(change)
            # Retain the base across target rewrites and Git garbage collection.
            self.workspace.run(
                ["update-ref", "refs/mcp-drafts/" + result["change_id"], result["base_commit"]]
            )
            return result

    async def submit_change(
        self,
        change_id: str,
        expected_diff_hash: str,
        mode: str = "review",
        wait_seconds: float = 15,
        *,
        actor: Principal | None = None,
    ) -> dict:
        actor = self.actor(actor)
        self.authorize(actor)
        if mode != "review":
            raise DomainError("PERMISSION_DENIED", "Only review mode is supported")
        if not 0 <= wait_seconds <= 25:
            raise DomainError("INVALID_LIMIT", "wait_seconds must be between 0 and 25")
        change = await asyncio.to_thread(self._submit, change_id, expected_diff_hash, actor)
        self.wake.set()
        deadline = time.monotonic() + wait_seconds
        while (
            self.task
            and not self.task.done()
            and time.monotonic() < deadline
            and change["status"] in {"queued", "validating", "committed", "pushed"}
        ):
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            change = await asyncio.to_thread(self.store.get, change_id, actor.subject)
        return self.envelope(change)

    def _submit(self, id: str, diff_hash: str, actor: Principal):
        for _ in range(5):
            change = self.store.get(id, actor.subject)
            if change["diff_hash"] != diff_hash:
                raise DomainError("DIFF_MISMATCH", "Submit requires the exact prepared diff hash")
            if change["submitted"] or change["status"] == "no_change":
                return change
            if change["status"] != "prepared":
                raise DomainError("CHANGE_STATE", "This draft cannot be submitted")
            change.update(status="queued", submitted=True, mode="review")
            try:
                self.store.save(change, "queued")
                return change
            except DomainError as error:
                if not error.retryable:
                    raise
        raise DomainError("CHANGE_STATE", "Concurrent update; retry submission", True)

    async def get_change(
        self, change_id: str, include_diff: bool = False, *, actor: Principal | None = None
    ) -> dict:
        actor = self.actor(actor)
        self.reader.policy.authorize(actor, "read")
        change = await asyncio.to_thread(self.store.get, change_id, actor.subject)
        self.wake.set()
        return self.envelope(change, diff=include_diff)

    async def list_changes(
        self,
        status: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
        *,
        actor: Principal | None = None,
    ) -> dict:
        actor = self.actor(actor)
        self.reader.policy.authorize(actor, "read")
        self.reader.validate_limit(limit)
        if status is not None and status not in set(ChangeStatus):
            raise DomainError("INVALID_FILTER", "Unknown change status")
        fingerprint = hashlib.sha256(
            json.dumps(["changes", actor.subject, status, limit]).encode()
        ).hexdigest()
        after = None
        if cursor:
            # Reuse signed cursor machinery; this list uses a stable creation-time keyset.
            after, _ = self.reader.decode_cursor(cursor, fingerprint, None)
        rows = await asyncio.to_thread(self.store.all, actor.subject, status)
        if after:
            try:
                stamp, id = json.loads(after)
                rows = [c for c in rows if (c["created_at"], c["change_id"]) < (stamp, id)]
            except (ValueError, TypeError):
                raise DomainError("INVALID_CURSOR", "Invalid change list cursor") from None
        items = []
        next_cursor = None
        snapshot = self.reader.index.snapshot()
        for change in rows[:limit]:
            candidate = items + [self.public(change)]
            token = self.reader.cursor(
                json.dumps([change["created_at"], change["change_id"]]), 0, fingerprint
            )
            try:
                self.reader.envelope({"items": candidate, "next_cursor": token}, snapshot)
            except DomainError:
                if not items:
                    raise
                break
            items, next_cursor = candidate, token
        return self.reader.envelope(
            {"items": items, "next_cursor": next_cursor if len(rows) > len(items) else None},
            snapshot,
        )

    async def cancel_change(self, change_id: str, *, actor: Principal | None = None) -> dict:
        actor = self.actor(actor)
        self.authorize(actor)
        for _ in range(5):
            change = await asyncio.to_thread(self.store.get, change_id, actor.subject)
            if change.get("merged") or change["status"] in {
                "indexed",
                "no_change",
                "closed",
                "updated",
            }:
                return self.envelope(change)
            change["cancel_requested"] = True
            if not change["push_intent"] or change["cr_number"]:
                change["status"] = "cancelled"
            change["message"] = (
                "Automation cancelled. Any published branch and PR remain on Forgejo; "
                "this does not close or revert them."
            )
            try:
                await asyncio.to_thread(self.store.save, change, "cancel_requested")
                self.wake.set()
                return self.envelope(change)
            except DomainError as error:
                if not error.retryable:
                    raise
        raise DomainError("CHANGE_STATE", "Concurrent update; retry cancellation", True)

    async def start(self):
        self.stopping = False
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.stopping = True
        self.wake.set()
        if self.task:
            await asyncio.shield(self.task)

    async def run(self):
        while not self.stopping:
            self.wake.clear()
            await self.run_once()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.settings.write_poll_seconds)
            except TimeoutError:
                pass

    async def run_once(self):
        await asyncio.to_thread(self._run_once)

    def _run_once(self):
        with file_lock(self.settings.data_root / ".write-worker.lock"):
            for change in self.store.all():
                if (
                    not change["submitted"]
                    or change["status"]
                    in {
                        "indexed",
                        "closed",
                        "failed",
                        "needs_attention",
                        "blocked_policy",
                        "updated",
                    }
                    or (change["status"] == "cancelled" and not change["cr_number"])
                    or (
                        change.get("parent_change_id")
                        and change["status"] == "cancelled"
                        and not change["push_intent"]
                    )
                    or time.time() < change.get("next_attempt", 0)
                ):
                    continue
                try:
                    self._step(change)
                except DomainError as error:
                    if error.code == "CHANGE_STATE" and error.retryable:
                        continue  # Another request updated the journal; reconcile on the next tick.
                    change.update(code=error.code, message=error.message)
                    if error.retryable:
                        change["attempts"] += 1
                        change.update(
                            status="merged" if change["merged"] else "retry_wait",
                            next_attempt=time.time() + min(300, 2 ** min(change["attempts"], 8)),
                        )
                    else:
                        change["status"] = (
                            "blocked_policy"
                            if error.code
                            in {"PERMISSION_DENIED", "POLICY_CHANGED", "WRITE_DISABLED"}
                            else "needs_attention"
                        )
                    try:
                        self.store.save(change, "error")
                    except DomainError as stale:
                        if stale.code != "CHANGE_STATE":
                            raise
                except (OSError, sqlite3.DatabaseError):
                    # Keep the journal at its last durable intent. Never infer a remote failure.
                    continue

    def _step(self, change: dict):
        if change.get("parent_change_id"):
            self._step_update(change)
            return
        # Observation remains available with WRITE_ENABLED=false. New effects are reauthorized.
        if change["merged"]:
            self._index_merged(change)
            return
        # Reconcile already authorized, unknown effects even after revocation/cancellation.
        # Discovery is read-only; a missing effect never grants permission to repeat it.
        if change["cr_intent"] and not change["cr_number"]:
            change["forge_evidence"] = asyncio.run(self.adapter.verify())
            found = asyncio.run(self.adapter.find(change))
            if found:
                self._record_pr(change, found)
                return
        if change["push_intent"] and not change["pushed"]:
            with self.source.lock():
                self.source.initialize()
                remote_head = self.workspace.remote_head(change["branch"])
            if remote_head is not None:
                if remote_head != change["head_commit"]:
                    raise DomainError("BRANCH_CHANGED", "Published branch has an unexpected head")
                change.update(pushed=True, status="pushed")
                self.store.save(change, "push_reconciled")
        if change["cancel_requested"] and not change["cr_number"]:
            change["status"] = "cancelled"
            self.store.save(change, "cancelled_after_reconciliation")
            return
        if change["cr_number"]:
            pr = asyncio.run(self.adapter.get_cr(change["cr_number"]))
            self.adapter.validate(pr, change)
            change.update(
                provider_head_commit=pr["head"]["sha"],
                cr_state=pr["state"],
                merged=pr["merged"],
                code=None,
                message=None,
            )
            if pr["merged"]:
                change.update(
                    status="merged",
                    merge_commit=pr["merge_commit_sha"],
                    merged_by=(pr.get("merged_by") or {}).get("login"),
                )
                self.store.save(change, "merged_observed")
                self._index_merged(change)
            else:
                change["status"] = (
                    "closed"
                    if pr["state"] == "closed"
                    else ("cancelled" if change["cancel_requested"] else "awaiting_review")
                )
                change["next_attempt"] = time.time() + self.settings.write_poll_seconds
                self.store.save(change, "review_observed")
            return
        if not self.owner_allowed(change["owner"]):
            raise DomainError(
                "PERMISSION_DENIED", "Initiator is no longer permitted to publish changes"
            )
        if (
            change["policy_hash"] != self.settings.policy_hash
            or change["target_branch"] != self.source.branch
        ):
            raise DomainError("POLICY_CHANGED", "Policy or target changed; prepare a new draft")
        if not change["head_commit"]:
            change["status"] = "validating"
            self.store.save(change, "commit_intent")
            with self.source.lock():
                target, _, _ = (
                    self.source._sync()
                )  # Fresh target; stale read fallback is not allowed.
                change["target_commit"] = target
                if target != change["base_commit"]:
                    change["warnings"] = [
                        "Target advanced; PR retains the exact approved base and intent. "
                        "Forgejo merges against the current target during review."
                    ]
                change["head_commit"] = self.workspace.commit(change)
            change["status"] = "committed"
            self.store.save(change, "committed")
        if not change["pushed"]:
            # Verify the configured repository/schema before the first remote write.
            change["forge_evidence"] = asyncio.run(self.adapter.verify())
            if change["cancel_requested"] and not change["push_intent"]:
                change["status"] = "cancelled"
                self.store.save(change, "cancelled")
                return
            change["push_intent"] = True
            self.store.save(change, "push_intent")
            with self.source.lock():
                self.workspace.publish(change)
            change.update(pushed=True, status="pushed")
            self.store.save(change, "pushed")
        if change["cancel_requested"] and not change["cr_intent"]:
            change["status"] = "cancelled"
            self.store.save(change, "cancelled_after_push")
            return
        change["cr_intent"] = True
        self.store.save(change, "cr_intent")
        pr = asyncio.run(self.adapter.create_or_find_cr(change))
        self._record_pr(change, pr)

    def _record_pr(self, change: dict, pr: dict):
        change.update(
            cr_number=pr["number"],
            cr_url=pr["html_url"],
            cr_state=pr["state"],
            provider_head_commit=pr["head"]["sha"],
            status="merged"
            if pr["merged"]
            else (
                "closed"
                if pr["state"] == "closed"
                else ("cancelled" if change["cancel_requested"] else "awaiting_review")
            ),
            merged=pr["merged"],
            merge_commit=pr.get("merge_commit_sha"),
            merged_by=(pr.get("merged_by") or {}).get("login"),
            next_attempt=0,
            attempts=0,
            code=None,
            message=None,
        )
        self.store.save(change, "cr_reconciled")
        if change["merged"]:
            self._index_merged(change)

    def _index_merged(self, change: dict):
        snapshot = self.reader._synchronize()
        if snapshot.stale:
            raise DomainError(
                "SYNC_UNAVAILABLE", "PR merged; target synchronization is pending", True
            )
        with self.source.lock():
            # Verify the API merge SHA, which can differ from the proposal after squash.
            reachable = (
                self.workspace.run(["rev-list", snapshot.source_commit]).decode().splitlines()
            )
            if change["merge_commit"] not in reachable:
                raise DomainError(
                    "SYNC_UNAVAILABLE", "Merge commit is not yet visible in target history", True
                )
        change.update(
            status="indexed",
            visible_in_read=True,
            next_attempt=0,
            code=None,
            message="PR merged and the target snapshot is available to read.",
        )
        self.store.save(change, "indexed")
