# Verification evidence — 2026-09-30

Version 0.2.0 is a locally verified M2 implementation candidate. P0-P2 below are
historical read evidence; P3/P4 now add real disposable Forgejo review tests.
Production deployment and ChatGPT/tunnel acceptance remain pending. No production
credentials/private vault or publication of the 0.2.0 candidate was used.

## P0

- Python project installs from `uv.lock`; FastMCP **3.4.7**, MCP SDK **1.30.0**,
  Pydantic Settings **2.13.1**, PyYAML **6.0.3**, py-key-value-aio **0.4.4**.
- Native checks used Python **3.12.3**; Docker runtime uses Python **3.12.12**.
- Public `GitHubProvider.verify_token`, middleware, `client_storage`, signing-key
  configuration, `set_mcp_path` and HTTP transport were inspected and exercised.
  No private token validator patch is used.
- OAuth scope `read:user` supplies stable `sub` from GitHub numeric user ID.
  The allowlist is checked on every verified HTTP token and every MCP request.
- Persistent encrypted DiskStore and generated keys survive provider recreation.
- Repository/API-root derivation has GitHub, nested GitLab and Forgejo fixtures;
  custom subpath metadata can be explicit. Actual vendor API identity remains unverified.
- Forgejo version/swagger, PR creation, atomic head precondition, approvals and
  branch protections remain **unknown**: no target test instance/repository or
  credentials were supplied. No merge capability is asserted; write is disabled.
- Official tunnel image registry manifest inspected and pinned to
  `sha256:119799b778ba8411a124f53588f9dc837fd62ba03e5fadf77d675123c92ab58e`,
  with amd64/arm64 manifests. This is registry evidence, not a live tunnel test.

## P1

- `ruff check .` and `ruff format --check .` pass.
- `pytest -q`: **22 passed**. Two upstream deprecation notices concern
  Starlette/Authlib's HTTPX interfaces; no test failure is suppressed.
- `uv build --offline` produces a standalone source distribution and wheel.
- Docker images built for Linux arm64 and amd64; amd64 runs through local emulation.
  Compose smoke covers HTTPS clone, authenticated Git, HTTP MCP search/pinned read,
  response revision, non-root mount access and persistence after force-recreate.
- Both architecture images pass OAuth discovery/S256 and unauthenticated POST 401.
- Local mocked-upstream OAuth tests exercise DCR, exact redirect rejection, consent,
  PKCE rejection, one-time code consumption, encrypted storage, restart, issuer/resource
  rejection, removed/disallowed IDs, upstream revocation and authenticated MCP search.
  Real GitHub OAuth App responses without refresh are tested separately from synthetic
  refresh-capable responses. A fresh login is required after expiry/revocation when
  the upstream has no refresh token.
- Compose environment keys match the parser. Tunnel credentials and Git credentials
  are isolated. OAuth host publishing is loopback-only; tunnel example has no ports.
- GitHub Actions builds/tests both architectures and publishes the exact tested
  images to `ghcr.io/fabiancz/markdown-mcp`. Workflow syntax is checked locally;
  the first GitHub run passed Python checks and the amd64 Compose assertions,
  but failed to clean up Linux fixture files owned by UID 10001. The smoke script
  now restores ownership of only the disposable mounts after stopping Compose.
  The permission failure and ownership repair were reproduced in Linux containers
  for both architectures; the actual cleanup helper and Compose smoke also pass locally.
  Successful registry publication remains to be verified.
- `actionlint` **1.7.12** passes. Workflow metadata was exercised for PR/main,
  matching version tags/manual input and mismatched versions. Publication ordering
  and stable-only `latest` were exercised with a simulated Docker command.
  Local builds with OCI source/version labels pass Compose smoke for both architectures.

## P2

- Real temporary Git clone/fetch/tree/batch-object extraction; empty mounts initialize.
- Real disposable HTTPS smart-Git fixture with synthetic Basic credentials and trusted
  CA: clone/fetch succeeds, wrong PAT fails, secrets are absent from clone configuration,
  redirects are not followed and failed initial clone is not published.
- Czech accent/alias relevance; literal URL/IP/command/punctuation; filename; typed
  frontmatter/tag/folder filters; snippets matching original lines and byte revision.
- Add/delete/rename updates; unchanged SHA reuses index; concurrent queries share one
  sync; canceled waiter does not cancel the shared sync; dirty checkout is preserved.
- Restart/pinned snapshot/cursor persistence; offline fallback and strict errors;
  policy invalidation; bounded read continuation and expiry errors.
- Invalid YAML, unsafe/deep/large YAML, non-UTF8, LFS, oversized files, symlink exclusion,
  normalized path collisions and traversal rejection across read/outline/backlinks.
- Actual SQLite trigger failure proves generation rollback, including FTS. Corrupt
  derived-index recovery preserves state/auth markers. A retention race returns
  `SNAPSHOT_EXPIRED` instead of silently empty data; SQL read views are pinned.

Synthetic retrieval results are in [search-evaluation.json](search-evaluation.json):
48 Czech/English requests, top-5 recall and MRR 1.0 on 1,000 and 10,000 notes.
Unweighted BM25 also reaches top-5 recall 1.0 on this corpus with the same filters;
these fixtures do not establish a general superiority of the chosen weights.
Warm p95 is around 30 ms for 10,000 notes. This host is not constrained to 2 vCPU.

Separate [network measurements](search-network-evaluation.json) include real local
TLS Git: initial clone/fetch/index/read is approximately 0.46 s / 2.80 s for 1k/10k;
unchanged fetch/read p95 approximately 181 / 217 ms. WAN latency is absent.
These are synthetic developer-machine measurements, not a production-vault benchmark.

## Opt-in HTTP Git transport (2026-09-30)

`GIT_ALLOW_HTTP=true` permits credential-free HTTP repository/API URLs; HTTPS
remains the default. The credential helper matches the configured scheme, host,
port and repository path. Redirects remain disabled and OAuth origins require HTTPS.

Verification: locked dependency sync, Ruff lint/format and wheel/sdist build passed;
25 pytest cases passed, including real authenticated HTTP and HTTPS clone/fetch,
credential scoping, wrong-token failures and redirect rejection. Local Docker
builds and Compose smoke tests passed on Linux arm64 and emulated amd64 for both
transports and both profiles. Tunnel-profile MCP search/read and recreation
preserved the snapshot; OAuth discovery and unauthenticated 401 checks passed.
These tests used synthetic Git repositories and credentials. The HTTP change has
not been published to GHCR or tested against an operator's private deployment.

## Required external acceptance

Still unperformed: real ChatGPT HTTPS OAuth connect/search/read/reconnect, tunnel-only
workspace access and unauthorized-access denial, tunnel disconnect/restart recovery,
production Git/PAT/TLS connectivity, and vendor-specific PAT/API policy checks.
A public versioned registry image and license are also pending; the selected
namespace is `ghcr.io/fabiancz/markdown-mcp`.
The experimental tunnel example must not be advertised as a verified supported
installation until P0-T passes. See [deployment acceptance steps](deployment.md).

## P3/P4 — durable Forgejo review, 0.2.0

- `uv sync --locked`, Ruff lint/format and `uv build` passed.
- Standard suite: **59 passed, 3 opt-in live tests skipped**. Upstream HTTPX
  deprecation warnings remain visible.
- Actual bare-Git tests cover atomic operations, revisions, path safety, dirty
  service checkout, private staging, target advancement, foreign branch/worktree
  preservation, concurrent reservation and crashes after commit/push/PR effects.
- Durable retry reconciles unknown remote effects, including after cancellation
  or revocation. CAS tests cover cancellation during POST. Post-merge index failure
  preserves truthful merge acceptance and retries only synchronization.
- Hooks and repository-defined content filters never run, including a clean
  filter normally triggered by `git status`. No-op drafts produce no publication.
- Provider contract tests use a subset of real Forgejo 15.0.9 Swagger. They cover
  capped pagination, closed/merged discovery, duplicate prevention, branch/repo/
  marker identity, unavailable/error mapping, credential redaction and redirects.
- `uv run python scripts/forgejo_smoke.py --application-image markdown-mcp:m2-arm64
  --platform linux/arm64` passed all **3 live tests**, repeated successfully for
  `markdown-mcp:m2-amd64` and `linux/amd64` (local emulation).
- The script uses official Forgejo **15.0.9+gitea-1.22.0**, digest
  `sha256:91a5310c86934339e16bd06b6078aada836e3d8935b2d70f6598108cbfaed5d1`,
  a regular non-admin fixture owner and a product PAT scoped to `write:repository`.
  Fixture repository creation/deletion uses the synthetic account's separate
  fixture setup credentials. No repository/PR creation rights beyond the intended
  product workflow are silently assumed for deployment.
- Live tests exercise Git publication, real PR creation followed by an injected
  timeout, restart/reconciliation to one PR, manual PR metadata/branch updates,
  manual squash merge and read visibility of both atomically proposed notes;
  manual close/cancel and wrong PAT. Merge is performed by the test reviewer,
  never the product adapter. HTTP tools in both built MCP images preserve the
  same proposal/PR after container recreation and do not index an open PR.
- Both Linux image builds passed. Compose smoke passed on both architectures
  with HTTPS and opt-in HTTP Git: search/read/recreation in the tunnel profile,
  OAuth discovery/S256 and unauthenticated 401 in the OAuth profile.

The repeatable commands and precise limitations are in [write.md](write.md).
Only review is supported. Automatic merge/approvals/head-precondition capability
remain unverified/disabled. Other Forgejo versions, production connectivity/PAT/
branch protections and actual ChatGPT/tunnel acceptance require operator tests.
No commit, push, registry publication or deployment was performed by this work.

## Review feedback and same-PR updates — 0.2.2 (2026-10-01)

- Locked dependency sync, Ruff lint/format and wheel/sdist build passed. Standard
  suite: **72 passed, 4 opt-in live tests skipped**; upstream Starlette/Authlib
  deprecation warnings remain visible.
- Bare-Git tests cover multiple sequential updates of the same PR, immutable
  original previews, exact hashes/revisions, idempotency/no-op updates, current
  branch reads and preservation of manual reviewer commits. A real competing
  push injected after the last head check is rejected by the exact Git lease.
- Queued updates stop on closed/merged PRs or parent cancellation. Unknown pushes
  are reconciled after restart and cancellation/write revocation without replay.
  Parallel prepared updates cannot overwrite each other. Owner/path restrictions,
  changed review cursors and preview response limits are exercised.
- Live fixtures use Forgejo **15.0.9+gitea-1.22.0**, a regular owner and separate
  reviewer on private synthetic repositories. The product PAT is restricted to
  one repository with `write:repository` and `read:issue`; fixture setup/cleanup
  use separate synthetic Basic credentials.
- Live tests read discussion/review/inline feedback, preserve provider commit and
  position context, reject stale updates, preserve manual commits, recover after
  a real push followed by an injected timeout, and index a manual squash merge.
  The fixture waits for Forgejo's asynchronous mergeability computation; the
  product never merges. Discussion and inline endpoints return one collection,
  while the review list paginates. New inline comments can have empty original
  commit metadata; the adapter preserves the provider value.
- Local Linux arm64 and emulated amd64 images build successfully. Compose smoke
  passes for HTTPS and opt-in HTTP Git on both architectures, including tunnel
  read/recreation and OAuth discovery/S256/unauthenticated 401.
- `uv run python scripts/forgejo_smoke.py --application-image
  markdown-mcp:test-0.2.2-arm64 --platform linux/arm64` passes all **4 live tests**;
  the corresponding amd64 command also passes all **4**. HTTP MCP tools read PR
  context, prepare/submit an update, and preserve its same PR/head after another
  container recreation.

See [write.md](write.md) for the new tool sequence, token scope and concurrency
limits. These are local synthetic tests, not acceptance of an operator's actual
Forgejo/ChatGPT/tunnel installation. The checks above were completed before the
release commit/tag and registry publication. Pull the versioned image only after
its publishing workflow succeeds. No production deployment was performed.
