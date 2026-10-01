# Product development

This is an independent product repository. All code, tests, packaging, CI and
public documentation must work from this checkout alone. Use English for public
text, comments and Conventional Commit messages. Synthetic Czech search fixtures
are allowed. Never commit vaults, credentials, databases or runtime state.

Use Python 3.12 and uv with the committed lockfile. Run:

- `uv sync --locked`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run pytest`
- `uv build`
- `uv run python scripts/forgejo_smoke.py` for write/provider changes (Docker required)
- Add `--application-image IMAGE --platform linux/ARCH` to that smoke command
  when changing the write runtime; it verifies HTTP tools and container recreation.
- `uv run python scripts/evaluate.py` when changing search ranking or parsing
- `docker build -t ghcr.io/fabiancz/markdown-mcp:latest .` for runtime/container changes
- `actionlint .github/workflows/build.yml` for workflow changes
- `uv run python scripts/container_smoke.py --platform linux/ARCH --image IMAGE`
  for each architecture when changing container/release behavior
- Repeat the container smoke command with `--git-http` when changing Git transport
  or credential handling, to verify the opt-in HTTP path as well as HTTPS

GitHub Actions runs checks on hosted Ubuntu and publishes tested amd64/arm64
images to GHCR. Pull requests must never publish or receive package write access.
Stable image tags must match the package version and lockfile. Keep the product
workflow independent of private operator tools outside this checkout.

Keep MCP tools thin. Read immutable commit objects; never reset, commit or push
external changes. Fail closed on auth/configuration errors. Authenticate before
accessing the vault. Use public FastMCP extension points only. OAuth state and
keys must survive restarts. Index rebuild must preserve all other state.

Check the repository root before Git operations. Do not automatically commit or
publish. Record verification evidence and external limitations honestly.

Write is Forgejo review only. Keep prepared payloads/diffs immutable. Persist an
intent before commit/push/PR creation; reconcile unknown outcomes before retry,
including after cancellation or revocation. Never push a target or overwrite an
existing foreign branch. Keep merge acceptance and index visibility separate.
Live fixtures must use disposable loopback repositories with synthetic content;
never test writes against a production vault. Git plumbing bypasses filters,
hooks and merge drivers; even status must disable configured content filters.
