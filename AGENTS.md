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
- `uv run python scripts/evaluate.py` when changing search ranking or parsing
- `docker build -t ghcr.io/fabiancz/markdown-mcp:latest .` for runtime/container changes
- `actionlint .github/workflows/build.yml` for workflow changes
- `uv run python scripts/container_smoke.py --platform linux/ARCH --image IMAGE`
  for each architecture when changing container/release behavior

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
