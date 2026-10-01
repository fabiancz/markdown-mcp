"""Versioned domain contracts shared by transport and services."""

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    INVALID_OPERATION = "INVALID_OPERATION"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DIFF_MISMATCH = "DIFF_MISMATCH"
    CHANGE_NOT_FOUND = "CHANGE_NOT_FOUND"
    CHANGE_STATE = "CHANGE_STATE"
    WRITE_DISABLED = "WRITE_DISABLED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    BRANCH_CHANGED = "BRANCH_CHANGED"
    POLICY_CHANGED = "POLICY_CHANGED"
    INVALID_CONTENT = "INVALID_CONTENT"
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
    QUEUED = "queued"
    VALIDATING = "validating"
    COMMITTED = "committed"
    PUSHED = "pushed"
    AWAITING_REVIEW = "awaiting_review"
    RETRY_WAIT = "retry_wait"
    MERGED = "merged"
    INDEXED = "indexed"
    NO_CHANGE = "no_change"
    CANCELLED = "cancelled"
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
    def get(self, change_id: str, owner: str | None = None) -> dict: ...
    def reserve(self, change: dict) -> dict: ...
    def save(self, change: dict, event: str) -> None: ...


class GitWorkspace(Protocol):
    def prepare(self, base: str, operations: list[dict]) -> dict: ...
    def commit(self, change: dict) -> str: ...
    def publish(self, change: dict) -> None: ...


class ForgeAdapter(Protocol):
    def capabilities(self) -> dict[str, bool | None]: ...
    async def create_or_find_cr(self, change: dict) -> dict: ...
    async def get_cr(self, number: int) -> dict: ...
    async def verify(self) -> dict: ...
    async def find(self, change: dict) -> dict | None: ...
    def validate(self, pr: dict, change: dict) -> dict: ...
