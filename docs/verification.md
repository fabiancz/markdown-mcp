# Verification evidence — 2026-09-30

This is a locally verified M1 implementation candidate. Production M1 is pending
actual ChatGPT and deployment acceptance. No production credentials, private vault,
forge write operation or public image publication was used.

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

## Required external acceptance

Still unperformed: real ChatGPT HTTPS OAuth connect/search/read/reconnect, tunnel-only
workspace access and unauthorized-access denial, tunnel disconnect/restart recovery,
production Git/PAT/TLS connectivity, and vendor-specific PAT/API policy checks.
A public versioned registry image and license are also pending; the selected
namespace is `ghcr.io/fabiancz/markdown-mcp`.
The experimental tunnel example must not be advertised as a verified supported
installation until P0-T passes. See [deployment acceptance steps](deployment.md).
