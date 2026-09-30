"""Atomic SQLite generations with durable raw note text and FTS5."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .config import Settings
from .models import DomainError, Snapshot
from .parser import parse_note
from .policy import Policy, normalize, path_key


def now() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
 id TEXT PRIMARY KEY, commit_sha TEXT NOT NULL, indexed_at TEXT NOT NULL,
 checked_at TEXT, created REAL NOT NULL, size INTEGER NOT NULL, warnings TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (
 id INTEGER PRIMARY KEY, snapshot TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 path TEXT NOT NULL, document TEXT NOT NULL, UNIQUE(snapshot,path));
CREATE INDEX IF NOT EXISTS notes_snapshot ON notes(snapshot,path);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
 title, aliases, path, tags, headings, body, snapshot UNINDEXED,
 tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS links (
 snapshot TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 source TEXT NOT NULL, target TEXT, line INTEGER NOT NULL, details TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS links_target ON links(snapshot,target,source);
"""


class Index:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_root / "index.sqlite"
        self.build_count = 0
        try:
            self.initialize()
        except sqlite3.DatabaseError:
            # Only the derived index is replaceable. Preserve the damaged file for diagnosis.
            suffix = f".corrupt-{time.time_ns()}"
            for extra in ("", "-wal", "-shm"):
                path = self.path.with_name(self.path.name + extra)
                if path.exists():
                    path.rename(path.with_name(path.name + suffix))
            self.initialize()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Invalid derived index")
            db.executescript(SCHEMA)
            stored = dict(db.execute("SELECT key,value FROM config").fetchall())
            if stored.get("schema", "1") != "1":
                raise DomainError(
                    "SCHEMA_UNSUPPORTED", "Index schema is newer than this application"
                )
            if stored.get("source", self.settings.source_identity) != self.settings.source_identity:
                raise DomainError("SOURCE_MISMATCH", "Index belongs to another repository")
            if stored.get("policy", self.settings.policy_hash) != self.settings.policy_hash:
                db.execute("DELETE FROM snapshots")
                db.execute("DELETE FROM notes_fts")
                db.execute("DELETE FROM config WHERE key='active'")
            for key, value in {
                "schema": "1",
                "source": self.settings.source_identity,
                "policy": self.settings.policy_hash,
            }.items():
                db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (key, value))
            db.execute("INSERT INTO notes_fts(notes_fts) VALUES ('integrity-check')")
        self.path.chmod(0o600)

    def active(self) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM config WHERE key='active'").fetchone()
            return row[0] if row else None

    def snapshot(self, id: str | None = None) -> Snapshot:
        id = id or self.active()
        with self.connect() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id=?", (id,)).fetchone()
            active = db.execute("SELECT value FROM config WHERE key='active'").fetchone()
        if row is None or (
            active
            and id != active[0]
            and time.time() - row["created"] > self.settings.snapshot_retention_seconds
        ):
            raise DomainError("SNAPSHOT_EXPIRED", "Search again to obtain an available snapshot")
        return Snapshot(
            snapshot_id=row["id"],
            source_commit=row["commit_sha"],
            indexed_at=row["indexed_at"],
            source_checked_at=row["checked_at"],
            warnings=json.loads(row["warnings"]),
        )

    def mark_checked(self, id: str, warnings: list[str]):
        with self.connect() as db:
            row = db.execute("SELECT warnings FROM snapshots WHERE id=?", (id,)).fetchone()
            stable = [w for w in json.loads(row[0]) if w != "DIRTY_CHECKOUT_IGNORED"]
            db.execute(
                "UPDATE snapshots SET checked_at=?,warnings=? WHERE id=?",
                (now(), json.dumps(sorted(set(stable + warnings))), id),
            )

    def publish(self, commit: str, files: list[tuple[str, bytes]], warnings: list[str]) -> str:
        id = (
            "snap_"
            + hashlib.sha256(
                (self.settings.source_identity + commit + self.settings.policy_hash).encode()
            ).hexdigest()
        )
        policy = Policy(self.settings.allowed_folders, self.settings.excluded_folders)
        documents = [parse_note(path, raw) for path, raw in files if policy.permits(path)]
        paths = {path_key(d["path"]): d["path"] for d in documents}
        names: dict[str, set[str]] = {}
        for d in documents:
            for name in [PurePosixPath(d["path"]).stem, d["title"], *d["aliases"]]:
                names.setdefault(normalize(name), set()).add(d["path"])
        size = sum(len(json.dumps(d).encode()) for d in documents)
        if size > self.settings.snapshot_max_bytes:
            raise DomainError(
                "INDEX_LIMIT", "Snapshot exceeds the configured retention storage limit"
            )
        timestamp = now()
        with self.connect() as db:
            db.execute("DELETE FROM notes_fts WHERE snapshot=?", (id,))
            db.execute("DELETE FROM snapshots WHERE id=?", (id,))
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?)",
                (
                    id,
                    commit,
                    timestamp,
                    timestamp,
                    time.time(),
                    size,
                    json.dumps(sorted(set(warnings))),
                ),
            )
            for d in documents:
                row = db.execute(
                    "INSERT INTO notes(snapshot,path,document) VALUES (?,?,?)",
                    (id, d["path"], json.dumps(d, ensure_ascii=False)),
                )
                db.execute(
                    (
                        "INSERT INTO "
                        "notes_fts(rowid,title,aliases,path,tags,headings,body,snapshot) "
                        "VALUES (?,?,?,?,?,?,?,?)"
                    ),
                    (
                        row.lastrowid,
                        d["title"],
                        " ".join(d["aliases"]),
                        d["path"],
                        " ".join(d["tags"]),
                        " ".join(h["title"] for h in d["headings"]),
                        d["text"],
                        id,
                    ),
                )
                for link in d["links"]:
                    target = resolve_link(d["path"], link, paths, names)
                    details = dict(link, resolved=target is not None)
                    db.execute(
                        "INSERT INTO links VALUES (?,?,?,?,?)",
                        (id, d["path"], target, link["line"], json.dumps(details)),
                    )
            db.execute("INSERT OR REPLACE INTO config VALUES ('active',?)", (id,))
            rows = db.execute(
                "SELECT id,created,size FROM snapshots WHERE id<>? ORDER BY created DESC", (id,)
            ).fetchall()
            retained_size = size
            for row in rows:
                retained_size += row["size"]
                if (
                    time.time() - row["created"] > self.settings.snapshot_retention_seconds
                    or retained_size > self.settings.snapshot_max_bytes
                ):
                    db.execute("DELETE FROM notes_fts WHERE snapshot=?", (row["id"],))
                    db.execute("DELETE FROM snapshots WHERE id=?", (row["id"],))
        self.build_count += 1
        return id

    @staticmethod
    def read_transaction(db: sqlite3.Connection, id: str):
        # Pin the SQL read view while selecting data, even if retention concurrently prunes it.
        db.execute("BEGIN")
        if db.execute("SELECT 1 FROM snapshots WHERE id=?", (id,)).fetchone() is None:
            raise DomainError("SNAPSHOT_EXPIRED", "Search again to obtain an available snapshot")

    def note(self, id: str, path: str) -> dict:
        with self.connect() as db:
            self.read_transaction(db, id)
            row = db.execute(
                "SELECT document FROM notes WHERE snapshot=? AND path=?", (id, path)
            ).fetchone()
        if row is None:
            raise DomainError(
                "NOTE_NOT_FOUND", "No readable note exists at that path in this snapshot"
            )
        return json.loads(row[0])


def resolve_link(source: str, link: dict, paths: dict, names: dict) -> str | None:
    target = unquote(link["target"])
    if urlsplit(target).scheme or target.startswith("//") or "\\" in target or "\0" in target:
        return None
    if not target:
        return source
    if link["kind"] == "markdown":
        parts = list(PurePosixPath(source).parent.parts)
        for p in target.split("/"):
            if p == "..":
                if not parts:
                    return None
                parts.pop()
            elif p not in (".", ""):
                parts.append(p)
        candidate = "/".join(parts)
        return paths.get(path_key(candidate))
    candidate = target if target.lower().endswith(".md") else target + ".md"
    exact = paths.get(path_key(candidate))
    if exact:
        return exact
    relative = paths.get(path_key(str(PurePosixPath(source).parent / candidate)))
    if relative:
        return relative
    matches = names.get(normalize(target), set())
    return next(iter(matches)) if len(matches) == 1 else None
