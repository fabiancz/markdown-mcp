# Durable Forgejo review writes

Version 0.2.0 implements local proposals and Forgejo pull requests. Review is the
only mode. The server never merges a PR, pushes the target, rewrites an existing
remote branch, or calls an external model. GitHub/GitLab remain read providers.

## Setup and compatibility

Enable `WRITE_ENABLED=true`, keep `WRITE_DEFAULT_MODE=review` and
`YOLO_ENABLED=false`, and set `GIT_PROVIDER=forgejo`. Use a regular service account
with access to the vault and a PAT with `write:repository`. The same PAT is used
for Git HTTPS and API `Authorization: token ...`. Repository creation/deletion
are fixture setup actions, not required product permissions. OAuth also requires
`GITHUB_WRITE_USER_IDS`, an explicit subset of the numeric read allowlist; the
GitHub OAuth App and Git PAT remain separate. Everyone admitted to the tunnel
shares the local operator's enabled rights.

The default API root is `https://HOST/api/v1`; override `FORGE_API_URL` and
`FORGE_REPO_ID` for installations under a subpath. Git/API endpoints must be
reachable from the application container. `GIT_ALLOW_HTTP=true` also applies to
API URLs; HTTPS is the default, with optional `GIT_CA_BUNDLE`. Redirects are never
followed and environment proxies are not used for the API client.

Live compatibility was verified on **Forgejo 15.0.9+gitea-1.22.0**, using the
official 15.0.9 image and its `/swagger.v1.json`. Before the first publication,
the adapter reads `/version`, the configured repository and the instance schema,
checking required review endpoints/fields. Other versions require their own
integration acceptance. Automatic merge, atomic merge-head preconditions and
approval capabilities remain unsupported/unverified by this release.

The adapter uses GET repository/version/schema, paginated GET pulls with
`state=all`, POST pulls and GET pull-by-number. Source/base repository IDs, refs
and the durable `<!-- markdown-mcp:chg_... -->` body marker identify a PR.
Discovery includes closed/merged PRs and continues until an empty page, even when
the server caps the requested page size. A mismatching or ambiguous branch/marker
stops publication. Descriptions contain paths, actor ID, validation, base/hash and
warnings; no note body is sent to an unrelated service.

API reference: [Forgejo API usage](https://forgejo.org/docs/latest/user/api/usage/).
The source schema subset used by contract tests is in
`tests/fixtures/forgejo-15.0.9-review.json`.

## Tools and operations

All tools require the current identity; a change ID does not grant access to
another owner's proposal. Responses use the same schema/request/meta envelope as
read. `meta` describes the current read snapshot, while the change's base snapshot
and Git SHA are separate fields in `data`.

```text
prepare_change(operations, summary, idempotency_key, base_snapshot_id)
submit_change(change_id, expected_diff_hash, mode="review", wait_seconds=15)
get_change(change_id, include_diff=false)
list_changes(status=null, limit=10, cursor=null)
cancel_change(change_id)
```

Use the exact `snapshot_id` returned by read/search and SHA-256 `revision` over the
original note bytes. Operation examples:

```json
[
  {"op":"create","path":"notes/New.md","content":"# New\n"},
  {"op":"replace","path":"notes/Existing.md","expected_revision":"SHA256","content":"# Updated\n"},
  {"op":"delete","path":"notes/Old.md","expected_revision":"SHA256"},
  {"op":"rename","path":"notes/Source.md","destination":"notes/Destination.md","expected_revision":"SHA256"}
]
```

A transaction accepts distinct, non-overlapping regular Markdown paths. It
rejects symlinks/submodules, symlink/file ancestors, traversal, excluded folders,
case/Unicode collisions, occupied destinations and incorrect revisions. New text
must be bounded UTF-8 without NUL, conflict markers or invalid frontmatter.
Renames preserve bytes and return readable backlinks for explicit follow-up
operations; links are not rewritten automatically.

The proposal returns its full Git diff, SHA-256 `diff_hash`, paths, base, warnings
and backlink references. Default limits are 50 operations, 1 MiB normalized
request/diff and 1 MiB per file. The full preview must also fit the response budget
(default 32 KiB); otherwise preparation returns `RESPONSE_LIMIT` before reserving
the key. Increase `MAX_RESPONSE_BYTES` deliberately for larger previews.

Idempotency binds owner/repository/key to the normalized operations, trimmed
summary and base snapshot ID. Distinct operation ordering is normalized. Retry
returns the same proposal even when its old read snapshot has since expired;
changing the payload returns `IDEMPOTENCY_CONFLICT`. Submit binds the immutable
proposal's exact diff hash and accepts review only. No-op proposals return
`no_change` without a commit, push or PR.

`submit_change` queues the persistent job and waits at most 25 seconds (default
15). A queued/validating/committed/pushed/retry result is not confirmation of a PR.
Use `get_change` and `cr_url`; lists use signed creation-order keyset cursors and
include only the current owner's changes. `include_diff=true` retrieves the same
prepared preview. Read annotations remain read-only; prepare/cancel modify local
state, and submit has remote effects. Annotations do not authorize callers.

## Git and state guarantees

`data/state.sqlite` stores immutable intent, original/result blobs, payload hash,
branch, diff hash, actor, timestamps, transitions and audit. WAL transactions,
unique owner/repository/key reservation and compare-and-swap updates prevent lost
cancellations. The state file must be backed up; it is not part of the derived
index. Corrupt/foreign state is never silently rebuilt as an empty job journal.

A serialized worker runs independently of HTTP calls and resumes after startup.
Git mutations/fetch share a thread/process repo lock; API polling does not hold
that lock. One active process per installation is the supported deployment.
Retries persist backoff (up to 300 seconds); permanent policy/provider/branch
errors stop in `blocked_policy` or `needs_attention`. Prepare a fresh proposal
when a terminal draft is no longer publishable; repeat submission is not a reset.

Commits use `mcp/<slug>-<unique_id>` branches, a private index and raw Git blobs.
Only explicitly declared files are staged; unchanged objects are retained.
Worktrees under `repo/worktrees/<change_id>` show the affected resulting files;
they intentionally do not checkout the entire vault. Hooks, content filters,
external diffs and merge drivers are bypassed. Commit dates/service identity and
change/actor trailers are saved for deterministic replay. Service checkout edits
and foreign draft edits are preserved, never reset or included in the proposal.

The worker fetches the target before committing. If it advanced, the proposal
retains the exact approved base and diff, with a warning; Forgejo's normal
three-way review merge preserves compatible concurrent changes and exposes
conflicts. The worker does not adapt whole-file content behind the preview. A
conflicting proposal can still have a valid PR for human resolution. An open PR
or unpublished proposal never becomes part of ordinary read snapshots.

Before each remote effect the worker saves intent. Initial push uses an empty
expected-ref lease to create only a missing bot branch atomically; despite the
Git option's name this cannot replace an existing ref. An equal existing SHA
confirms a prior push; a different SHA stops without overwriting. Before retrying
an unknown PR POST, the adapter discovers the existing marker/branch PR. Errors
and timeouts never imply the effect failed. After cancellation/revocation, unknown
already-authorized effects are discovered read-only; missing effects are not
repeated without current permission.

Manual reviewer commits are observed through `provider_head_commit` and are
never replaced with the original head. After a manual merge, the worker stores
`merged=true`, actual merge SHA/merger, fetches/indexes the target and verifies
that its history contains the merge SHA. Only then is `visible_in_read=true`.
Index failure preserves the truthful merged state and retries synchronization
without another publication or merge.

Statuses include prepared, queued, validating, committed, pushed,
awaiting_review, retry_wait, merged, indexed, no_change, cancelled, closed,
blocked_policy and needs_attention. `accepted`, `cr_open`, `merged` and
`visible_in_read` are independent facts. Provider errors include
`PROVIDER_UNAVAILABLE` (retryable unknown/unavailable result), `PROVIDER_REJECTED`,
`PERMISSION_DENIED`, `BRANCH_CHANGED`; draft errors include `INVALID_OPERATION`,
`INVALID_CONTENT`, `REVISION_MISMATCH`, `DIFF_MISMATCH`, `IDEMPOTENCY_CONFLICT`,
`POLICY_CHANGED`, `WRITE_DISABLED`, `CHANGE_STATE` and `CHANGE_NOT_FOUND`.

## Cancellation, disablement and recovery

Cancellation before publication stops the draft. If a push/POST was in flight,
the worker first discovers its result. Existing branches and PRs remain on
Forgejo: cancellation does not close, delete or revert them. Close a PR manually
on Forgejo. The worker can still observe a merge of an already published cancelled
PR. A detected closed PR is terminal; reopening it is outside automated monitoring.

Set `WRITE_ENABLED=false` to prevent new publication. Existing status tools and
observation remain available when the change journal exists. OAuth revocation or
removing an ID from the writer allowlist stops not-yet-performed publication;
observation/index repair do not impersonate a newly authorized writer.

Back up both mounts and configuration with the service stopped, or use consistent
SQLite backup operations. Preserve `state.sqlite`, its WAL, `source.json`, auth
keys, Git objects/refs and worktrees together. Index rebuild may replace only
`index.sqlite`. Branch/source/API changes require a deliberate new deployment or
migration; pending proposals must not silently change their repository/target.

Drafts, audit and worktrees are retained without automatic age-based deletion in
this release. Do not remove unresolved/unknown effects or run a blanket worktree
cleanup. Storage compaction/retention is a future operator feature. No PR body
marker or bot branch should be edited away until its outcome is recorded.

## Repeatable verification

```sh
uv run pytest
uv run python scripts/forgejo_smoke.py
uv run python scripts/forgejo_smoke.py --application-image LOCAL_IMAGE --platform linux/arm64
```

The smoke script starts a pinned disposable Forgejo, creates a regular synthetic
account and repositories, runs the live contract/restart/review tests, and removes
its container/anonymous volumes. It never needs production credentials. The image
option adds actual HTTP prepare/submit and MCP container recreation with durable
state. Repeat with amd64 to cover the emulated architecture.
