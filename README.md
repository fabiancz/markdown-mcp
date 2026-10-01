# Markdown Vault MCP

A self-hosted Python/FastMCP server for a single Git-backed Markdown vault.
Search, list, read, outline and backlinks use immutable commit snapshots and
SQLite FTS5. No running Obsidian, embedding service or external AI API is needed.

Version 0.2.0 adds durable writes through Forgejo pull requests for review.
Version 0.2.2 adds PR discussion/review reads and approved updates to the same PR.
Read works with GitHub, GitLab and Forgejo repositories. Write is opt-in;
automatic merge is disabled. Obsidian does not need to run on the server.

## Installation

You need Docker Engine with Docker Compose and a Git repository containing your
Markdown notes. The application clones the repository automatically; you do not
need Python, a local source checkout or an existing vault clone on the server.

Choose how your AI client will connect:

| Variant | When to use it | Requirements |
| --- | --- | --- |
| **OAuth over HTTPS** | For clients supporting remote MCP over Streamable HTTP and OAuth, such as ChatGPT or Claude custom connectors | A public HTTPS endpoint, a GitHub OAuth App and an allowed GitHub account |
| **OpenAI Secure MCP Tunnel** | For supported OpenAI clients while keeping the MCP server inside your private network | An OpenAI tunnel, an API key and permission to use the tunnel |

GitHub OAuth is used to sign in to the MCP server. It is independent of where
your notes are hosted: you can sign in with GitHub and read a GitLab or Forgejo
repository. Client compatibility depends on its transport and OAuth support;
this project does not claim verified compatibility with every MCP client.

Claude's remote connectors require a publicly reachable server; see
[Claude's custom connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

### Prepare your installation directory

Create a directory for the deployment and two empty persistent directories:

```sh
mkdir -p markdown-mcp/repo markdown-mcp/data
cd markdown-mcp
sudo chown 10001:10001 repo data
chmod 700 repo data
```

Copy the Compose file and environment example for your chosen variant, using the
links in the following sections. Save them as `docker-compose.yaml` and `.env`:

```text
markdown-mcp/
├── docker-compose.yaml
├── .env
├── repo/
└── data/
```

The application runs as UID/GID `10001:10001`, so both directories must be
writable by that user. It stores the managed Git clone in `repo/checkout/` and
the search index and persistent application state in `data/`.

### Configure your Git repository

Edit these settings in `.env` for either variant:

```dotenv
IMAGE=ghcr.io/fabiancz/markdown-mcp:0.2.2
GIT_PROVIDER=forgejo
GIT_REPO_URL=https://forge.example/owner/vault.git
GIT_USERNAME=your-service-account
GIT_PAT=your-repository-read-token
GIT_TARGET_BRANCH=
```

- Set `GIT_PROVIDER` to `github`, `gitlab` or `forgejo`.
- Use the repository's HTTPS clone URL without credentials embedded in it.
- Provide an account and PAT with read access to that repository.
- Leave `GIT_TARGET_BRANCH` empty to use the repository's default branch.
- Keep write disabled for read-only use. To enable Forgejo review, follow the write section below.

For a Git server on a trusted private network without HTTPS, you can explicitly
enable HTTP:

```dotenv
GIT_REPO_URL=http://192.0.2.10:3000/owner/vault.git
GIT_ALLOW_HTTP=true
```

The host and port must be reachable from the container. HTTP sends credentials
and note content unencrypted. SSH repository URLs are currently unsupported.

Use the 0.2.2 image after its publishing workflow has completed successfully.
It includes Forgejo review writes, feedback reads, same-PR updates and HTTP transport support. Earlier read
images do not include the write tools. For local testing, build this checkout.

The examples use `:latest`, which is created by stable release publication; pushes to `main`
publish `sha-...` tags. Choose an existing tag from
[container packages](https://github.com/fabiancz/markdown-mcp/pkgs/container/markdown-mcp)
and see [release instructions](https://github.com/fabiancz/markdown-mcp/blob/main/docs/releases.md)
for tag details.

### OAuth

Use these files:

- [docker-compose.yaml](https://github.com/fabiancz/markdown-mcp/blob/main/examples/oauth/docker-compose.yaml)
- [.env.example](https://github.com/fabiancz/markdown-mcp/blob/main/examples/oauth/.env.example)

1. Configure a domain and HTTPS reverse proxy for the MCP server. The Compose
   service listens on `127.0.0.1:8000` on the Docker host by default. Forward all
   paths, including OAuth discovery and callbacks, to this service. An
   [nginx example](https://github.com/fabiancz/markdown-mcp/blob/main/examples/nginx/mcp.conf)
   is included.
2. Create a GitHub OAuth App. Set its homepage to your public origin and its
   authorization callback URL to `https://YOUR_HOST/auth/callback`.
3. Fill in the OAuth settings in `.env`:

```dotenv
PUBLIC_BASE_URL=https://mcp.example.com
GITHUB_OAUTH_CLIENT_ID=your-oauth-app-client-id
GITHUB_OAUTH_CLIENT_SECRET=your-oauth-app-client-secret
GITHUB_ALLOWED_USER_IDS=123456
OAUTH_REDIRECT_URIS=https://your-client.example/oauth/callback
```

`GITHUB_ALLOWED_USER_IDS` is a comma-separated list of permitted numeric GitHub
user IDs, not usernames. `OAUTH_REDIRECT_URIS` must contain the exact HTTPS
callback URL(s) used by your MCP client. The URL above is a placeholder; replace
it with your client's actual callback. The GitHub OAuth App secret and the Git
repository PAT are separate credentials.

Start the service:

```sh
chmod 600 .env
docker compose pull
docker compose up -d
docker compose logs --tail=100 mcp
```

Add `https://YOUR_HOST/mcp` as a remote MCP connection in your AI client and
complete GitHub sign-in and consent with an allowed account.

### Tunnel

Use these files:

- [docker-compose.yaml](https://github.com/fabiancz/markdown-mcp/blob/main/examples/tunnel/docker-compose.yaml)
- [.env.example](https://github.com/fabiancz/markdown-mcp/blob/main/examples/tunnel/.env.example)

This variant runs the MCP server and OpenAI's tunnel client in two containers.
It uses outbound HTTPS to OpenAI, so you do not need a public domain, an HTTPS
reverse proxy or inbound internet ports for the MCP server.

1. Create a tunnel in [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels).
   Associate it with the Platform organization and ChatGPT workspace that will
   use it, and copy its tunnel ID.
2. Create an [OpenAI API key](https://platform.openai.com/api-keys) in that
   organization. The tunnel operator needs **Tunnels Read + Use** permission.
3. Fill in the tunnel settings in `.env`:

```dotenv
OPENAI_TUNNEL_ID=tunnel_...
OPENAI_TUNNEL_API_KEY=sk-...
```

Keep the pinned `TUNNEL_IMAGE` from the example. `OPENAI_TUNNEL_API_KEY` is an
OpenAI API key used by the tunnel client; it is separate from `GIT_PAT`.

Start both services:

```sh
chmod 600 .env
docker compose pull
docker compose up -d
docker compose logs --tail=100 mcp tunnel
```

In ChatGPT, create a developer-mode MCP connection, choose **Tunnel**, and select
your tunnel or enter its ID. Developer-mode access is separate from Platform
tunnel permissions. Other supported OpenAI clients use their tunnel connection
settings. See the [OpenAI Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

This profile needs no additional GitHub OAuth sign-in. Everyone allowed to use
the tunnel receives the same access to the configured vault, including write
when `WRITE_ENABLED=true`. Keep the MCP
port private, as in the supplied Compose file. Deployment verification status is
documented in [verification evidence](https://github.com/fabiancz/markdown-mcp/blob/main/docs/verification.md).

## Using the server

The first startup clones your repository and builds the index. Subsequent
requests fetch remote changes by default.

A typical workflow is:

1. `search_notes(query="zaloha")` returns paths, revisions and line snippets.
2. `read_note(path=..., snapshot_id=...)` reads the matching version of the note.
3. Continue a long note with `next_start_line` and the same snapshot ID.

Fulltext handles Czech accents, words and quoted phrases. Use `literal` for
commands, URLs or punctuation, and `filename` for paths, titles and aliases.
Stemming and semantic similarity are not included.

For Forgejo review writes, use a service account with repository access and a
PAT scoped to `write:repository` and `read:issue` for PR feedback reads. Enable:

```dotenv
WRITE_ENABLED=true
WRITE_DEFAULT_MODE=review
YOLO_ENABLED=false
```

In OAuth mode also set `GITHUB_WRITE_USER_IDS` to the permitted subset of
`GITHUB_ALLOWED_USER_IDS`. The tunnel profile uses one shared `tunnel_operator`.

1. Read/search to obtain `snapshot_id` and each existing note's `revision`.
2. Call `prepare_change` with create/replace/delete/rename operations, a summary,
   a unique idempotency key and `base_snapshot_id`. Inspect the returned diff/hash.
3. Call `submit_change(change_id=..., expected_diff_hash=..., mode="review")`.
4. Follow `cr_url` for review and manual merge; poll `get_change` for progress.
   Read sees the proposal only after merge into the configured target.

To address feedback in that PR, call `get_change_review` with the original change
ID, then `read_change_note` with its exact `head_sha`. Prepare operations using
`prepare_change_update` with `expected_head_sha` and the returned note revisions.
Inspect the new diff, then call `submit_change_update` with its `update_id` and
exact `diff_hash`. Poll `get_change(update_id)` for `applied=true`. A changed head
requires a fresh preview; manual reviewer commits are preserved. With write
enabled, 0.2.2 advertises sixteen tools; fresh read-only installations keep seven.

Accepted, PR-open, merged and visible-in-read are separate facts. Restart/retry
reconciles existing effects; it does not create a second PR or overwrite foreign
branch changes. `cancel_change` stops further publication and leaves any existing
PR/branch on Forgejo. It does not close a PR or undo a merge. See the complete
[write workflow](docs/write.md), including limits and recovery.

For updates, pull your chosen image and recreate the services with
`docker compose pull` and `docker compose up -d`. Preserve `repo/`, `data/` and
`.env`; back them up before upgrades. Run one active application instance per
installation.

See [deployment and recovery](https://github.com/fabiancz/markdown-mcp/blob/main/docs/deployment.md),
[environment reference](https://github.com/fabiancz/markdown-mcp/blob/main/docs/configuration.md)
and [tool contracts](https://github.com/fabiancz/markdown-mcp/blob/main/docs/contracts.md).

## Develop

Python 3.12, Git and uv are required. Tests use temporary synthetic repositories
and mock GitHub responses, including disposable HTTP and HTTPS smart-Git servers.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
uv run python scripts/evaluate.py
uv run python scripts/forgejo_smoke.py
docker build -t ghcr.io/fabiancz/markdown-mcp:latest .
uv run python scripts/container_smoke.py --platform linux/arm64
uv run python scripts/container_smoke.py --platform linux/arm64 --git-http
```

The container smoke commands need Docker Engine and `host.docker.internal`
resolution from containers. Linux hosts need that name mapped to the host gateway.
Repeat container tests for `linux/amd64` with an image built for that architecture.
GitHub Actions tests both amd64 and arm64 on hosted Ubuntu runners before
publishing the exact tested images to GHCR. Pull requests verify only; `main`
publishes commit images, and version tags/manual dispatch publish releases. See
[release instructions](https://github.com/fabiancz/markdown-mcp/blob/main/docs/releases.md).
