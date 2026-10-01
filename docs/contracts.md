# Read and domain contracts

All data tools return a versioned envelope:

```json
{
  "schema_version": "1",
  "request_id": "unique-request-id",
  "data": {"items": []},
  "meta": {
    "snapshot_id": "snap_sha256",
    "source_commit": "git-commit-sha",
    "indexed_at": "UTC ISO timestamp",
    "source_checked_at": "UTC ISO timestamp",
    "stale": false,
    "warnings": []
  }
}
```

Every search/list hit carries a path, title, stable path-derived note ID, SHA-256
`revision` of the exact original bytes, `source_commit`, `snapshot_id`, tags and
parse warnings. Search adds mode, deterministic ranking score and up to three
exact source substrings with 1-based line ranges. Score is an ordering value,
not a probability. A truncated snippet may contain only part of a long line.

Tools are `search_notes`, `list_notes`, `read_note`, `get_note_outline`,
`get_backlinks`, `get_vault_status` and a harmless `ping` connectivity probe.
All tools require the current allowed identity. `ping` has no note data.

Since 0.2.1, `ping.data` includes `status`, deployment `mode`, application `version`,
`write_enabled` and `write_default_mode`, without fetching Git. MCP initialization
reports the same installed package version in `serverInfo.version`; it is separate
from the response `schema_version` and MCP protocol version.

Read tools are read-only/non-destructive; freshness can fetch the configured
remote and update local cache. When write is enabled or a durable change journal
exists, review tools are also registered; their contracts are in [write.md](write.md).

`search_notes` defaults to fulltext ALL terms; words and balanced quoted phrases
are the entire query language. Raw FTS operators, unquoted punctuation, prefix
wildcards and malformed quotes produce `INVALID_QUERY`. Punctuation inside a
phrase is tokenized by FTS; use `literal` for exact punctuation. Literal substring
matching is case-insensitive and scans stored text. Filename mode searches paths,
titles and aliases. Fulltext uses unicode61/remove_diacritics=2, BM25 weights
8/6/5/4/3/1 (title/aliases/path/tags/headings/body), with exact title/path/alias
matches ranked first. Fuzzy, regex, semantic and hybrid are unavailable.

Folder filters require an exact directory boundary. Tags use AND. Frontmatter
filters use scalar equality and preserve JSON value types, including the
boolean/number distinction. Filters run before pagination. Defaults are 10 hits,
maximum 50, 500 query characters and a 32 KiB encoded response envelope. Long read
responses stop at whole source lines and return `next_start_line`; a single line
that exceeds the budget raises `RESPONSE_LIMIT`. Outline is bounded by the same
budget and can return that error for very large outlines.

Cursors are HMAC-signed and bind the query, limit, snapshot and policy. A changed
parameter or modified cursor returns `INVALID_CURSOR`. Restart preserves cursor
keys. Explicit snapshots and cursors skip fetch. Prior generations expire after
15 minutes by default; storage pressure may expire them earlier. Active generation
is retained. Expired IDs return `SNAPSHOT_EXPIRED`, never different text. A changed
path/index policy immediately invalidates older snapshots and cursors.

Wikilinks, anchors, block IDs and embeds are stored passively. Embeds are never
expanded. Markdown relative links resolve within the readable snapshot. Ambiguous
note names stay unresolved. Hidden sources never appear in backlinks or ranking.
Invalid YAML preserves original text with `INVALID_FRONTMATTER`; YAML aliases,
unsafe constructors, excessive size/depth/nodes are rejected. Non-UTF8, LFS,
oversized and non-regular Markdown are skipped with aggregate diagnostics.

Errors are MCP tool results with `isError=true` and a JSON error body, for example:

```json
{"code":"INVALID_CURSOR","message":"Cursor does not match the query, snapshot and policy","retryable":false}
```

Codes include `INVALID_QUERY`, `INVALID_FILTER`, `INVALID_PATH`, `INVALID_RANGE`,
`INVALID_LIMIT`, `NOTE_NOT_FOUND`, `SNAPSHOT_EXPIRED`, `UNSUPPORTED_SEARCH_MODE`,
`RESPONSE_LIMIT`, `SYNC_UNAVAILABLE`, `SOURCE_MISMATCH`, `INDEX_LIMIT`,
`PATH_COLLISION`, `INDEX_FAILED`, `STORAGE_UNAVAILABLE`, `SCHEMA_UNSUPPORTED`,
`AUTH_STATE_INVALID` and `PERMISSION_DENIED`. HTTP auth rejection remains a 401,
not a successful data response. Messages never include credentials or hidden paths.

`models.py` separates authorization, snapshot, reader, change-store, workspace
and forge-adapter contracts. Durable writes are scoped to owner/repository and
expose accepted, PR-open, merged and read visibility independently. See
[the write contract](write.md) for operations, states and errors.

Version 0.2.2 adds `get_change_review`, `read_change_note`,
`prepare_change_update` and `submit_change_update`. Review pagination binds to
the PR head and comment content; changing either requires restarting pagination.
Branch reads carry `source=pr_head` and `head_sha` in `data` separately from the
target snapshot in `meta`. Updates have their own `update_id`, `parent_change_id`,
immutable diff and independent `applied` result (`status=updated`). They are
included in `get_change`/`list_changes`. `HEAD_CHANGED` rejects stale PR updates;
`REVIEW_CHANGED` rejects changed review cursors. These additions retain envelope
schema version 1 and the existing state-store schema.
