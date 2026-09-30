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
| Push `vX.Y.Z` | `X.Y.Z`, `X.Y.Z-amd64`, `X.Y.Z-arm64`, `latest` |
| Manual dispatch on `main` | Current package version (or matching input), architecture tags, `latest` |

Manual dispatch on another branch verifies/builds but does not publish. Stable
release versions must be `X.Y.Z`, without leading zeros, and match
`[project].version` in `pyproject.toml`. A mismatched tag/input fails before
publication. Main builds never move `latest`. Each stable release moves `latest`,
including a manually republished or older release. Examples use `latest`; choose an
exact version when you want to pin a deployment.

## Issue a release

From this product checkout, update package metadata and the lockfile together:

```sh
uv version --no-sync 0.1.1
```

Review and commit the version change, then push the commit and its tag using your
normal Git workflow. A tag such as `v0.1.1` starts publication. Alternatively,
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
`IMAGE=ghcr.io/fabiancz/markdown-mcp:0.1.1`, after that version exists.
Run `docker compose pull` and `docker compose up -d` using the existing mounts.
License selection and real ChatGPT/tunnel acceptance remain separate release tasks.

See [GitHub's Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
for visibility, authentication and package permissions.
