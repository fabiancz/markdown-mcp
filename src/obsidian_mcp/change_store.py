"""Durable change journal; intent is saved before external effects."""

import json
import sqlite3
import time
from contextlib import contextmanager

from .config import Settings
from .models import DomainError


class SQLiteChangeStore:
    def __init__(self, settings: Settings):
        self.path = settings.data_root / "state.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.repo_id = settings.source_identity
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS write_config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS changes (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, repo TEXT NOT NULL,
                    idem TEXT NOT NULL, hash TEXT NOT NULL, created REAL NOT NULL,
                    status TEXT NOT NULL, document TEXT NOT NULL, UNIQUE(owner,repo,idem));
                CREATE TABLE IF NOT EXISTS change_audit (
                    sequence INTEGER PRIMARY KEY, change_id TEXT NOT NULL,
                    at REAL NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL);
            """)
            binding = json.dumps(
                [settings.source_identity, settings.api_url, settings.repository_id]
            )
            stored = dict(db.execute("SELECT key,value FROM write_config"))
            if stored.get("schema", "1") != "1":
                raise DomainError("SCHEMA_UNSUPPORTED", "Unsupported persistent change schema")
            if stored.get("binding", binding) != binding:
                raise DomainError("SOURCE_MISMATCH", "Change journal belongs to another repository")
            db.executemany(
                "INSERT OR IGNORE INTO write_config VALUES (?,?)",
                [("schema", "1"), ("binding", binding)],
            )
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def find(self, owner: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT document FROM changes WHERE owner=? AND repo=? AND idem=?",
                (owner, self.repo_id, key),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def get(self, id: str, owner: str | None = None) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT owner,document FROM changes WHERE id=?", (id,)).fetchone()
        if not row or (owner is not None and row[0] != owner):
            raise DomainError("CHANGE_NOT_FOUND", "No change is available to this identity")
        return json.loads(row[1])

    def reserve(self, change: dict) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT document FROM changes WHERE owner=? AND repo=? AND idem=?",
                (change["owner"], self.repo_id, change["idempotency_key"]),
            ).fetchone()
            if row:
                prior = json.loads(row[0])
                if prior["request_hash"] != change["request_hash"]:
                    raise DomainError(
                        "IDEMPOTENCY_CONFLICT", "Key already belongs to another request"
                    )
                return prior
            db.execute(
                "INSERT INTO changes VALUES (?,?,?,?,?,?,?,?)",
                (
                    change["change_id"],
                    change["owner"],
                    self.repo_id,
                    change["idempotency_key"],
                    change["request_hash"],
                    change["created_at"],
                    change["status"],
                    json.dumps(change),
                ),
            )
            self.audit(db, change, "prepared")
        return change

    @staticmethod
    def audit(db, change: dict, event: str):
        fields = [
            "owner",
            "parent_change_id",
            "paths",
            "base_commit",
            "head_commit",
            "merge_commit",
            "cr_url",
            "code",
        ]
        db.execute(
            "INSERT INTO change_audit(change_id,at,event,details) VALUES (?,?,?,?)",
            (
                change["change_id"],
                time.time(),
                event,
                json.dumps({k: change.get(k) for k in fields}),
            ),
        )

    def save(self, change: dict, event: str):
        previous = change.get("version", 0)
        change["version"] = previous + 1
        change["updated_at"] = time.time()
        with self.connect() as db:
            result = db.execute(
                "UPDATE changes SET status=?,document=? WHERE id=? "
                "AND COALESCE(json_extract(document,'$.version'),0)=?",
                (change["status"], json.dumps(change), change["change_id"], previous),
            )
            if result.rowcount != 1:
                raise DomainError("CHANGE_STATE", "Change was updated concurrently; retry", True)
            self.audit(db, change, event)

    def all(self, owner: str | None = None, status: str | None = None) -> list[dict]:
        clauses, params = ["repo=?"], [self.repo_id]
        if owner is not None:
            clauses.append("owner=?")
            params.append(owner)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        with self.connect() as db:
            rows = db.execute(
                "SELECT document FROM changes WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created DESC,id DESC",
                params,
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
