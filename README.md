# Obsidian Read MCP

A self-hosted Python/FastMCP server for a single Git-backed Markdown vault.
Search, list, read, outline and backlinks use immutable commit snapshots and
SQLite FTS5. No running Obsidian, embedding service or external AI API is needed.
This 0.1.1 implementation supports read only.

Deployment profiles:

- HTTPS with a GitHub OAuth App, numeric user-ID allowlist and persistent encrypted
  OAuth state. Git credentials are independent of the GitHub login.
- An isolated MCP container behind Secure MCP Tunnel, using the shared local
  principal `tunnel_operator`. The example does not publish MCP ports.

Read and OAuth flows are tested locally. Actual ChatGPT login and tunnel workspace
access still require deployment verification. The tunnel example is experimental
until that test passes. See [verification evidence](docs/verification.md).
The examples use `IMAGE=ghcr.io/fabiancz/markdown-mcp:latest`. Wait for a
successful release publication before pulling it, or build the same tag locally
as shown below.
The image namespace is `ghcr.io/fabiancz/markdown-mcp`; license selection remains
pending. See [container releases](docs/releases.md) for publishing and visibility.

## Run the test image

Build once as a developer:

```sh
docker build -t ghcr.io/fabiancz/markdown-mcp:latest .
```

Choose a profile and copy its `docker-compose.yaml` and `.env.example` into a new
installation directory. Rename `.env.example` to `.env`, fill in your HTTPS Git
URL, username and read-capable PAT, then fill in OAuth or tunnel configuration.
For a trusted private Git server using plain HTTP, explicitly set
`GIT_ALLOW_HTTP=true`; credentials and note content then travel unencrypted.
Prepare empty `repo/` and `data/` directories owned by UID/GID 10001:

```sh
mkdir -p repo data
sudo chown 10001:10001 repo data
chmod 700 repo data
chmod 600 .env
docker compose up -d
```

For OAuth, configure your HTTPS proxy and connect a client to
`https://YOUR_HOST/mcp`. Register the GitHub OAuth App callback as
`https://YOUR_HOST/auth/callback`. Set `OAUTH_REDIRECT_URIS` to the **exact callback
URL(s) provided by your MCP client**; the example client URL is a placeholder.
The first startup clones the target branch into `repo/checkout` and creates
`data/index.sqlite`. Both directories must be writable. Keep one active replica.

After an image is published, the examples track the latest stable release with
`IMAGE=ghcr.io/fabiancz/markdown-mcp:latest`. To update, run `docker compose pull`
and `docker compose up -d`. To pin a deployment, set `IMAGE` to a specific release
tag instead. Operators need no Python or local source build. This repository does
not claim a nonexistent registry image.

See [deployment and upgrade](docs/deployment.md), [environment reference](docs/configuration.md)
and [tool contracts](docs/contracts.md). A typical tool workflow is:

1. `search_notes(query="zaloha")` returns paths, revisions and line snippets.
2. `read_note(path=..., snapshot_id=...)` returns those same original lines.
3. Continue a long note with `next_start_line` and the same snapshot ID.

Fulltext handles Czech accents, words and quoted phrases, with ALL semantics.
It does not provide stemming or semantic similarity. Use `literal` for commands,
URLs or punctuation, and `filename` for paths, titles and aliases.

## Develop

Python 3.12, Git and uv are required. Tests use temporary synthetic repositories
and mock GitHub responses, including one disposable HTTPS smart-Git server.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
uv run python scripts/evaluate.py
uv run python scripts/container_smoke.py --platform linux/arm64
```

The last command needs Docker Engine and `host.docker.internal` resolution from
containers. Linux hosts need that name mapped to the host gateway. GitHub Actions
tests both amd64 and arm64 on hosted Ubuntu runners before publishing the exact
tested images to GHCR. Pull requests verify only; `main` publishes commit images,
and version tags/manual dispatch publish releases. See [release instructions](docs/releases.md).
