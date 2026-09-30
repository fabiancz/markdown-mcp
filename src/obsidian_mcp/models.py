"""Versioned domain contracts shared by transport and services."""

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    INVALID_CURSOR = "INVALID_CURSOR"
    INVALID_QUERY = "INVALID_QUERY"
    INVALID_FILTER = "INVALID_FILTER"
    INVALID_PATH = "INVALID_PATH"
    INVALID_RANGE = "INVALID_RANGE"
    INVALID_LIMIT = "INVALID_LIMIT"
    NOTE_NOT_FOUND = "NOTE_NOT_FOUND"
    SNAPSHOT_EXPIRED = "SNAPSHOT_EXPIRED"
    UNSUPPORTED_SEARCH_MODE = "UNSUPPORTED_SEARCH_MODE"
    RESPONSE_LIMIT = "RESPONSE_LIMIT"
    SYNC_UNAVAILABLE = "SYNC_UNAVAILABLE"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"
    INDEX_LIMIT = "INDEX_LIMIT"
    PATH_COLLISION = "PATH_COLLISION"
    INDEX_FAILED = "INDEX_FAILED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    AUTH_STATE_INVALID = "AUTH_STATE_INVALID"
    PERMISSION_DENIED = "PERMISSION_DENIED"


class DomainError(Exception):
    def __init__(self, code: ErrorCode | str, message: str, retryable: bool = False):
        self.code, self.message, self.retryable = ErrorCode(code), message, retryable
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class Principal(BaseModel):
    subject: str
    roles: frozenset[str] = frozenset({"reader"})


class Snapshot(BaseModel):
    snapshot_id: str
    source_commit: str
    indexed_at: str
    source_checked_at: str | None = None
    stale: bool = False
    warnings: list[str] = Field(default_factory=list)


class Envelope(BaseModel):
    schema_version: str = "1"
    request_id: str
    data: Any
    meta: Snapshot


class ChangeStatus(StrEnum):
    PREPARED = "prepared"
    PROCESSING = "processing"
    CR_OPEN = "cr_open"
    MERGED = "merged"
    NEEDS_ATTENTION = "needs_attention"
    BLOCKED_POLICY = "blocked_policy"
    FAILED = "failed"
    CLOSED = "closed"


class Change(BaseModel):
    change_id: str
    owner: str
    repo_id: str
    idempotency_key: str
    request_hash: str
    status: ChangeStatus
    base_commit: str
    head_commit: str | None = None
    cr_url: str | None = None
    visible_in_read: bool = False


class AuthPolicy(Protocol):
    def authorize(self, principal: Principal, operation: str) -> None: ...


class SnapshotSource(Protocol):
    def sync(self) -> tuple[str, list[tuple[str, bytes]], list[str]]: ...
    def files(self, commit: str) -> tuple[list[tuple[str, bytes]], list[str]]: ...


class VaultReader(Protocol):
    async def read_note(self, path: str, **kwargs: Any) -> dict: ...


class ChangeStore(Protocol):
    def get(self, change_id: str, owner: str) -> Change: ...
    def reserve(self, change: Change) -> Change: ...


class GitWorkspace(Protocol):
    def create_worktree(self, change_id: str, base_commit: str) -> str: ...


class ForgeAdapter(Protocol):
    def capabilities(self) -> dict[str, bool | None]: ...
    async def create_cr(self, branch: str, target: str, title: str) -> str: ...
    async def merge(self, cr_url: str, expected_head: str) -> dict: ...
