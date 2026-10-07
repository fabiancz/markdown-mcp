# Container releases on GitHub

The product source repository is [fabiancz/markdown-mcp](https://github.com/fabiancz/markdown-mcp).
Images are published to `ghcr.io/fabiancz/markdown-mcp`. Pull an image only after
its publishing workflow has completed successfully.

## Workflow

`.github/workflows/build.yml` runs on pull requests, pushes to `main`, version tags
and manual dispatch. It installs the locked dependencies, runs Ruff, pytest,
builds Python distributions and evaluates search. A container matrix builds and
tests Linux amd64 and arm64 with the real disposable HTTPS Git fixture and both
Compose profiles. Arm64 uses QEMU on the hosted Ubuntu runner.

Only after both architectures pass does the publishing job load the exact tested
images from temporary workflow artifacts, push architecture tags and combine
them into an OCI image index. Pull requests never receive registry write access
or publish images. Publication uses `GITHUB_TOKEN` with `packages: write`; no PAT
secret is needed in the repository. Actions are pinned to commit hashes.

| Trigger | Image tags |
| --- | --- |
| Pull request | No publication |
| Push to `main` | `sha-<first 12 commit characters>` and architecture suffixes |
| Push `vX.Y.ZrcN` (e.g. `v0.3.0rc1`) | `X.Y.ZrcN`, architecture suffixes; never `latest` |
| Push `vX.Y.Z` | `X.Y.Z`, `X.Y.Z-amd64`, `X.Y.Z-arm64`, `latest` |
| Manual dispatch on `main` | Current package version (or matching input), architecture tags, `latest` |

Manual dispatch on another branch verifies/builds but does not publish. Stable
release versions must be `X.Y.Z`; test candidates use `X.Y.ZrcN` with a positive
integer N. Both formats prohibit leading zeros and must match
`[project].version` in `pyproject.toml`. A mismatched tag/input fails before
publication. Main builds and release candidates never move `latest`. Each stable release moves `latest`,
including a manually republished or older release. Examples use `latest`; choose an
exact version when you want to pin a deployment.

## Issue a release

From this product checkout, update package metadata and the lockfile together:

```sh
uv version --no-sync 0.2.2
```

Review and commit the version change, then push the commit and its tag using your
normal Git workflow. A tag such as `v0.2.2` starts publication. Alternatively,
after pushing the updated version to `main`, run **Verify and publish containers**
from the Actions page and enter the matching version (or leave the input empty).
Do not reuse a released version for different code. Source/tag pushes and registry
publication are explicit operator actions.

After the first successful publication, open the package's settings and change
its visibility to **Public** if anonymous pulls are intended. New GHCR packages
start private even when the source repository is public. The OCI source label
links images to the source repository. If an existing package has different
access settings, grant this repository Actions access to the package.

The examples use `IMAGE=ghcr.io/fabiancz/markdown-mcp:latest`. To pin a deployment,
set `.env` to a released reference such as
`IMAGE=ghcr.io/fabiancz/markdown-mcp:0.2.2`, after that version exists.
Run `docker compose pull` and `docker compose up -d` using the existing mounts.
License selection and real ChatGPT/tunnel acceptance remain separate release tasks.

See [GitHub's Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
for visibility, authentication and package permissions.

## 0.2.1

MCP initialization now reports the application's installed package version,
matching `pyproject.toml`, the lockfile and its release tag. Previously it used
the FastMCP library default. Authenticated `ping` also reports the application
version and configured write policy to distinguish deployment configuration from
cached client tools. Upgrade and ChatGPT metadata refresh steps are documented in
[deployment](deployment.md). The 0.2.0 image remains unchanged. Pull 0.2.1 only
after its publishing workflow has completed successfully.

## 0.2.2

Adds discussion, review and inline feedback reads, current PR Markdown reads,
immutable update previews and durable submission of another commit to the same
PR. Exact head/revision/hash checks preserve manual reviewer commits and reject
stale updates. Unknown pushes are reconciled after restart without duplicate
commits or PRs. See [the review update workflow](write.md#read-feedback-and-update-the-same-pr-022).

Upgrade the Forgejo token to `write:repository` plus `read:issue`; it may remain
restricted to the selected vault repository. Existing state/mounts are reused
without a schema migration. Write-enabled MCP advertises sixteen tools, and its
installed version reports 0.2.2. Tag and image publication remain separate actions;
do not pull the 0.2.2 registry tag before its publishing workflow succeeds.

## 0.2.3

Advertises the bundled blue crystal icon in MCP initialization through
`serverInfo.icons`. The transparent 128×128 PNG is embedded as a data URI, so
both OAuth and tunnel profiles work without separate asset hosting or new
configuration. Refresh the client connection after upgrading. Icon rendering
depends on the client and has not been verified in ChatGPT. Existing mounts and
state are reused without a migration. Pull 0.2.3 only after its publishing
workflow succeeds.

## 0.3.0rc1 — attachment upload preview

Feature branch: `codex/attachments`. Git tag: `v0.3.0rc1`.
After its publishing workflow succeeds, use:

```dotenv
IMAGE=ghcr.io/fabiancz/markdown-mcp:0.3.0rc1
UPLOAD_MAX_FILE_BYTES=2000000
```

This candidate adds `upload_attachment` and `create_attachment` operations for
an atomic attachment/note PR or an update of the same PR. It advertises seventeen
tools with write enabled. Refresh client discovery after upgrade. File delivery
from an actual attached/generated ChatGPT file still needs
[operator acceptance](attachments.md#operator-acceptance-in-the-actual-ai-client).

Update the deployment Compose file to forward the upload settings; changing
`.env` alone does not add missing `environment` entries. Preserve both mounts and
use a synthetic test repository for the first check. Existing published 0.2.3
and `latest` are unaffected by a candidate tag. Every subsequent preview needs
a new version, for example `0.3.0rc2`; do not overwrite an existing candidate.

Publication runs in GitHub Actions using its scoped `GITHUB_TOKEN`. For a manual
local GHCR push, `docker login` without a registry logs into Docker Hub. Use
`docker login ghcr.io --username YOUR_GITHUB_USERNAME` and a GitHub classic PAT
with `write:packages` as the password; never place the token in a source file or
command argument. See [Docker login](https://docs.docker.com/reference/cli/docker/login/)
and [GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-to-the-container-registry).
