"""Read orchestration, validated query language and snapshot-bound cursors."""

import asyncio
import base64
import hashlib
import hmac
import json
import re
import sqlite3
import time
import uuid

from .config import Settings
from .git_source import ManagedGit
from .index import Index
from .models import DomainError, Envelope, Snapshot, SnapshotSource
from .policy import Policy, normalize, safe_path


def compile_query(query: str) -> tuple[str, list[str]]:
    terms = []
    position = 0
    for match in re.finditer(r'"([^"\n]+)"|(\w+)', query, re.UNICODE):
        if query[position : match.start()].strip():
            raise DomainError(
                "INVALID_QUERY", "Use words or quoted phrases; use literal mode for punctuation"
            )
        term = match[1] or match[2]
        words = re.findall(r"\w+", term)
        if not words:
            raise DomainError("INVALID_QUERY", "A phrase must contain searchable words")
        terms.append(" ".join(words))
        position = match.end()
    if query[position:].strip() or not terms:
        raise DomainError("INVALID_QUERY", "Use words or balanced quoted phrases")
    return " AND ".join('"' + term + '"' for term in terms), terms


def snippets(text: str, terms: list[str], literal: bool = False) -> list[dict]:
    lines = text.splitlines(keepends=True)
    hits, covered = [], set()
    for i, line in enumerate(lines):
        if i in covered:
            continue
        searchable = line.casefold() if literal else normalize(line)
        if not any(
            (term.casefold() if literal else normalize(term)) in searchable for term in terms
        ):
            continue
        start, end = max(0, i - 1), min(len(lines), i + 2)
        if covered.intersection(range(start, end)):
            start = i
        passage = "".join(lines[start:end])
        if len(passage) > 500:
            start, end, passage = i, i + 1, line
            # Keep an exact contiguous substring of this source line.
            at = next(
                (
                    normalize(line).find(normalize(t))
                    for t in terms
                    if normalize(t) in normalize(line)
                ),
                0,
            )
            passage = passage[max(0, at - 100) : max(0, at - 100) + 500]
        hits.append(
            {
                "line_start": start + 1,
                "line_end": end,
                "text": passage,
                "truncated": passage != "".join(lines[start:end]),
            }
        )
        covered.update(range(start, end))
        if len(hits) == 3:
            break
    return hits


class Reader:
    def __init__(self, settings: Settings, source: SnapshotSource | None = None):
        self.settings = settings
        self.source = source or ManagedGit(settings)
        self.index = Index(settings)
        self.policy = Policy(settings.allowed_folders, settings.excluded_folders)
        self.task: asyncio.Task | None = None
        self.last_sync = 0.0
        self.last_failure: str | None = None
        from .auth import persistent_secret

        self.cursor_key = persistent_secret(settings.data_root / "auth" / "cursor.key")

    def _synchronize(self) -> Snapshot:
        try:
            commit, _, warnings = self.source.sync()
            active = self.index.active()
            if active and self.index.snapshot(active).source_commit == commit:
                self.index.mark_checked(active, warnings)
            else:
                files, file_warnings = self.source.files(commit)
                active = self.index.publish(commit, files, warnings + file_warnings)
            self.last_failure = None
            self.last_sync = time.monotonic()
            return self.index.snapshot(active)
        except (DomainError, sqlite3.DatabaseError, OSError) as error:
            if isinstance(error, DomainError) and error.code in {
                "SOURCE_MISMATCH",
                "STORAGE_UNAVAILABLE",
                "SCHEMA_UNSUPPORTED",
            }:
                raise
            self.last_failure = error.code if isinstance(error, DomainError) else "SYNC_UNAVAILABLE"
            if self.settings.sync_failure_policy == "serve_stale" and self.index.active():
                result = self.index.snapshot()
                result.stale = True
                result.warnings.append(self.last_failure + ": serving the last successful snapshot")
                return result
            raise DomainError("SYNC_UNAVAILABLE", "No fresh snapshot is available", True) from None

    async def snapshot(self, snapshot_id: str | None = None) -> Snapshot:
        if snapshot_id:
            return await asyncio.to_thread(self.index.snapshot, snapshot_id)
        if self.index.active() and (
            (not self.settings.sync_before_read)
            or (time.monotonic() - self.last_sync < self.settings.sync_min_interval_seconds)
        ):
            return self.index.snapshot()
        # No await between checking and setting the shared task; a single event loop owns it.
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(asyncio.to_thread(self._synchronize))
        return await asyncio.shield(self.task)

    def envelope(self, data: dict, snapshot: Snapshot) -> dict:
        result = Envelope(request_id=uuid.uuid4().hex, data=data, meta=snapshot).model_dump()
        if len(json.dumps(result, ensure_ascii=False).encode()) > self.settings.max_response_bytes:
            raise DomainError("RESPONSE_LIMIT", "Request fewer results or a smaller line range")
        return result

    def cursor(self, snapshot: str, offset: int, fingerprint: str) -> str:
        payload = json.dumps(
            [snapshot, offset, fingerprint, self.settings.policy_hash], separators=(",", ":")
        ).encode()
        signed = hmac.digest(self.cursor_key, payload, "sha256") + payload
        return base64.urlsafe_b64encode(signed).decode()

    def decode_cursor(
        self, cursor: str, fingerprint: str, snapshot_id: str | None
    ) -> tuple[str, int]:
        try:
            if len(cursor) > 2048:
                raise ValueError()
            signed = base64.b64decode(cursor, altchars=b"-_", validate=True)
            signature, payload = signed[:32], signed[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.cursor_key, payload, "sha256")):
                raise ValueError()
            id, offset, query, policy = json.loads(payload)
            if (
                query != fingerprint
                or policy != self.settings.policy_hash
                or (snapshot_id is not None and id != snapshot_id)
                or type(offset) is not int
                or offset < 0
                or not isinstance(id, str)
            ):
                raise ValueError()
            return id, offset
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise DomainError(
                "INVALID_CURSOR", "Cursor does not match the query, snapshot and policy"
            ) from None

    async def page_snapshot(
        self, kind: str, params: dict, cursor: str | None, snapshot_id: str | None
    ):
        fingerprint = hashlib.sha256(
            json.dumps([kind, params], sort_keys=True).encode()
        ).hexdigest()
        offset = 0
        if cursor:
            snapshot_id, offset = self.decode_cursor(cursor, fingerprint, snapshot_id)
        return await self.snapshot(snapshot_id), offset, fingerprint

    @staticmethod
    def validate_limit(limit: int):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DomainError("INVALID_LIMIT", "Page limit must be between 1 and 50")

    def hit(self, document: dict, snapshot: Snapshot) -> dict:
        return {
            "id": "note_" + hashlib.sha256(document["path"].encode()).hexdigest(),
            "path": document["path"],
            "title": document["title"],
            "revision": document["revision"],
            "snapshot_id": snapshot.snapshot_id,
            "source_commit": snapshot.source_commit,
            "tags": document["tags"],
            "parse_warnings": document["warnings"],
        }

    def page(self, items: list, snapshot: Snapshot, offset: int, fingerprint: str, limit: int):
        selected = []
        for item in items[:limit]:
            tentative = {
                "items": selected + [item],
                "truncated": True,
                "next_cursor": self.cursor(
                    snapshot.snapshot_id, offset + len(selected) + 1, fingerprint
                ),
            }
            try:
                self.envelope(tentative, snapshot)
            except DomainError:
                if not selected:
                    raise
                break
            selected.append(item)
        more = len(items) > len(selected)
        return self.envelope(
            {
                "items": selected,
                "truncated": more,
                "next_cursor": self.cursor(
                    snapshot.snapshot_id, offset + len(selected), fingerprint
                )
                if more
                else None,
            },
            snapshot,
        )

    async def search_notes(
        self,
        query: str,
        mode: str = "fulltext",
        folder: str | None = None,
        tags: list[str] | None = None,
        frontmatter: dict | None = None,
        limit: int = 10,
        cursor: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict:
        self.validate_limit(limit)
        if mode not in ("fulltext", "literal", "filename"):
            raise DomainError(
                "UNSUPPORTED_SEARCH_MODE", "Available modes are fulltext, literal and filename"
            )
        if not query.strip() or len(query) > 500 or "\0" in query:
            raise DomainError("INVALID_QUERY", "Query must contain 1 to 500 characters")
        if folder is not None:
            folder = safe_path(folder, folder=True)
        tags = tags or []
        frontmatter = frontmatter or {}
        if (
            len(tags) > 50
            or len(frontmatter) > 50
            or any(type(v) not in (str, int, float, bool, type(None)) for v in frontmatter.values())
        ):
            raise DomainError("INVALID_FILTER", "Frontmatter supports scalar equality only")
        expression, terms = compile_query(query) if mode == "fulltext" else ("", [query])
        params = {
            "query": query,
            "mode": mode,
            "folder": folder,
            "tags": tags,
            "frontmatter": frontmatter,
            "limit": limit,
        }
        snapshot, offset, fingerprint = await self.page_snapshot(
            "search", params, cursor, snapshot_id
        )
        items = await asyncio.to_thread(
            self._search, snapshot, query, mode, folder, tags, frontmatter, expression, terms
        )
        return self.page(items[offset : offset + limit + 1], snapshot, offset, fingerprint, limit)

    def _search(self, snapshot, query, mode, folder, tags, frontmatter, expression, terms):
        with self.index.connect() as db:
            self.index.read_transaction(db, snapshot.snapshot_id)
            if mode == "fulltext":
                rows = db.execute(
                    (
                        "SELECT n.document,bm25(notes_fts,8,6,5,4,3,1) AS rank FROM "
                        "notes_fts JOIN notes n ON n.id=notes_fts.rowid WHERE notes_fts "
                        "MATCH ? AND n.snapshot=? ORDER BY rank,n.path"
                    ),
                    (expression, snapshot.snapshot_id),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT document,0 AS rank FROM notes WHERE snapshot=? ORDER BY path",
                    (snapshot.snapshot_id,),
                ).fetchall()
        items = []
        for row in rows:
            d = json.loads(row["document"])
            if folder and not d["path"].startswith(folder + "/"):
                continue
            if not {normalize(t.lstrip("#")) for t in tags}.issubset(d["tags"]):
                continue
            if any(
                k not in d["frontmatter"]
                or type(d["frontmatter"][k]) is not type(v)
                or d["frontmatter"][k] != v
                for k, v in frontmatter.items()
            ):
                continue
            fields = [d["title"], d["path"], d["path"].removesuffix(".md"), *d["aliases"]]
            exact = normalize(query) in [normalize(f) for f in fields]
            if mode == "filename" and not any(normalize(query) in normalize(f) for f in fields):
                continue
            if mode == "literal" and query.casefold() not in d["text"].casefold():
                continue
            score = (1000 if exact else 0) - row["rank"]
            hit = self.hit(d, snapshot)
            hit.update(
                match_type=mode, score=score, snippets=snippets(d["text"], terms, mode == "literal")
            )
            items.append(hit)
        items.sort(key=lambda item: (-item["score"], item["path"]))
        return items

    async def list_notes(
        self,
        folder: str | None = None,
        limit: int = 10,
        cursor: str | None = None,
        snapshot_id: str | None = None,
    ) -> dict:
        self.validate_limit(limit)
        if folder is not None:
            folder = safe_path(folder, folder=True)
        snapshot, offset, fingerprint = await self.page_snapshot(
            "list", {"folder": folder, "limit": limit}, cursor, snapshot_id
        )

        def load():
            with self.index.connect() as db:
                self.index.read_transaction(db, snapshot.snapshot_id)
                rows = db.execute(
                    (
                        "SELECT document FROM notes WHERE snapshot=? AND (? IS NULL OR "
                        "substr(path,1,length(?)+1)=?||'/') ORDER BY path LIMIT ? OFFSET ?"
                    ),
                    (snapshot.snapshot_id, folder, folder, folder, limit + 1, offset),
                ).fetchall()
            return [self.hit(json.loads(r[0]), snapshot) for r in rows]

        return self.page(await asyncio.to_thread(load), snapshot, offset, fingerprint, limit)

    async def readable_note(self, path: str, snapshot_id: str | None):
        safe_path(path)
        if not self.policy.permits(path):
            raise DomainError(
                "NOTE_NOT_FOUND", "No readable note exists at that path in this snapshot"
            )
        snapshot = await self.snapshot(snapshot_id)
        return snapshot, await asyncio.to_thread(self.index.note, snapshot.snapshot_id, path)

    async def read_note(
        self,
        path: str,
        snapshot_id: str | None = None,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict:
        if (
            type(start_line) is not int
            or start_line < 1
            or (end_line is not None and (type(end_line) is not int or end_line < start_line))
        ):
            raise DomainError("INVALID_RANGE", "Use a valid 1-based line range")
        snapshot, d = await self.readable_note(path, snapshot_id)
        lines = d["text"].splitlines(keepends=True)
        if start_line > max(1, len(lines)):
            raise DomainError("INVALID_RANGE", "Start line exceeds the note length")
        requested_end = min(end_line or len(lines), len(lines))
        selected, size = [], 0
        # Reserve room for JSON escaping, identity, metadata and response fields.
        budget = max(0, self.settings.max_response_bytes - 4096)
        for line in lines[start_line - 1 : requested_end]:
            line_size = len(json.dumps(line, ensure_ascii=False).encode())
            if size + line_size > budget:
                break
            selected.append(line)
            size += line_size
        if requested_end >= start_line and not selected:
            raise DomainError("RESPONSE_LIMIT", "A single line exceeds the response limit")
        actual_end = start_line + len(selected) - 1
        return self.envelope(
            dict(
                self.hit(d, snapshot),
                text="".join(selected),
                line_start=start_line,
                line_end=actual_end,
                line_count=len(lines),
                truncated=actual_end < requested_end,
                next_start_line=actual_end + 1 if actual_end < len(lines) else None,
            ),
            snapshot,
        )

    async def get_note_outline(self, path: str, snapshot_id: str | None = None) -> dict:
        snapshot, d = await self.readable_note(path, snapshot_id)
        return self.envelope(
            dict(
                self.hit(d, snapshot),
                headings=d["headings"],
                blocks=d["blocks"],
                line_count=d["line_count"],
                byte_count=d["byte_count"],
            ),
            snapshot,
        )

    async def get_backlinks(
        self, path: str, snapshot_id: str | None = None, limit: int = 10, cursor: str | None = None
    ) -> dict:
        self.validate_limit(limit)
        safe_path(path)
        snapshot, offset, fingerprint = await self.page_snapshot(
            "backlinks", {"path": path, "limit": limit}, cursor, snapshot_id
        )
        await self.readable_note(path, snapshot.snapshot_id)

        def load():
            with self.index.connect() as db:
                self.index.read_transaction(db, snapshot.snapshot_id)
                rows = db.execute(
                    (
                        "SELECT source,line,details FROM links WHERE snapshot=? AND "
                        "target=? ORDER BY source,line LIMIT ? OFFSET ?"
                    ),
                    (snapshot.snapshot_id, path, limit + 1, offset),
                ).fetchall()
            return [{"path": r[0], "line": r[1], "link": json.loads(r[2])} for r in rows]

        return self.page(await asyncio.to_thread(load), snapshot, offset, fingerprint, limit)

    async def get_vault_status(self) -> dict:
        # Diagnostics never trigger network IO.
        snapshot = await asyncio.to_thread(self.index.snapshot)
        if self.last_failure:
            snapshot.stale = True
            snapshot.warnings.append(self.last_failure)
        with self.index.connect() as db:
            self.index.read_transaction(db, snapshot.snapshot_id)
            count = db.execute(
                "SELECT count(*) FROM notes WHERE snapshot=?", (snapshot.snapshot_id,)
            ).fetchone()[0]
        return self.envelope(
            {
                "note_count": count,
                "source": "managed_git",
                "search_modes": ["fulltext", "literal", "filename"],
                "write_enabled": False,
                "limits": {
                    "file_bytes": self.settings.max_file_bytes,
                    "response_bytes": self.settings.max_response_bytes,
                    "page_hits": 50,
                    "query_characters": 500,
                },
                "snapshot_retention_seconds": self.settings.snapshot_retention_seconds,
            },
            snapshot,
        )
