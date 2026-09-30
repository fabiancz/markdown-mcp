# Environment reference

Compose reads `.env` and explicitly passes each service only its listed variables.
Application configuration reads the process environment, not an automatic local
`.env` file. Development can use `uv run --env-file .env obsidian-mcp`.

| Variables | Meaning / default |
|---|---|
| `IMAGE` | Compose-only, GHCR reference; examples use `ghcr.io/fabiancz/markdown-mcp:latest` for the latest stable release; a version tag can pin a deployment; selects only the `mcp` service image |
| `DEPLOYMENT_MODE` | Required explicit `oauth` or `tunnel`; fixed by each Compose example |
| `PUBLIC_BASE_URL` | Required HTTPS origin in OAuth mode, without subpath |
| `HOST_PORT` | OAuth Compose-only host port, default 8000 on 127.0.0.1 |
| `REPO_DIR`, `DATA_DIR` | Compose-only host directories, `./repo` and `./data` |
| `REPO_ROOT`, `DATA_ROOT` | Native app directory overrides, `/repo` and `/data`; Compose keeps these fixed |
| `HOST`, `PORT` | Native app listener, `0.0.0.0:8000` |
| `GIT_PROVIDER` | Required `github`, `gitlab` or `forgejo` |
| `GIT_REPO_URL` | Required credential-free HTTPS repository URL; no URL username/PAT/query/fragment |
| `GIT_TARGET_BRANCH` | Empty discovers/persists the remote default branch; explicit branch takes precedence |
| `GIT_USERNAME`, `GIT_PAT` | Required service-account HTTPS credentials; M1 requires repository read access only |
| `GIT_PAT_FILE` | Optional mounted secret file; conflicts with a nonempty `GIT_PAT` |
| `GIT_CA_BUNDLE` | Optional mounted CA certificate bundle; TLS verification cannot be disabled |
| `FORGE_REPO_ID` | Optional owner/repo or GitLab namespace/subgroup/project override |
| `FORGE_API_URL` | Optional HTTPS API root, required for ambiguous subpath hosting |
| `GIT_COMMIT_NAME`, `GIT_COMMIT_EMAIL` | Reserved future write metadata, defaults Vault MCP Bot / vault-mcp@example.invalid |
| `GIT_TIMEOUT_SECONDS` | 15 seconds per Git command |
| `SYNC_BEFORE_READ` | true; explicit snapshot/cursor skips synchronization |
| `SYNC_MIN_INTERVAL_SECONDS` | 0; positive values permit deliberate freshness caching |
| `SYNC_FAILURE_POLICY` | `serve_stale` (default) or `error`; no snapshot means error |
| `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET` | Required in OAuth mode; OAuth App credentials, separate from Git PAT |
| `GITHUB_OAUTH_CLIENT_SECRET_FILE` | Optional mounted secret file, conflicts with a nonempty direct secret |
| `GITHUB_ALLOWED_USER_IDS` | Required comma-separated numeric stable GitHub IDs in OAuth mode |
| `OAUTH_REDIRECT_URIS` | Required comma-separated exact HTTPS MCP-client callback URLs in OAuth mode |
| `ALLOWED_FOLDERS` | Empty permits all otherwise eligible Markdown; comma-separated relative directory roots |
| `EXCLUDED_FOLDERS` | `.git,.obsidian,.trash,repo,data`; relative roots, plus built-in rejection of Git/Obsidian/trash path components |
| `MAX_FILE_BYTES` | 1 MiB; oversize notes are skipped with diagnostic |
| `MAX_RESPONSE_BYTES` | 32 KiB per JSON envelope |
| `MAX_INDEX_BYTES` | 256 MiB raw selected Markdown per generation; at most 100,000 eligible notes |
| `SNAPSHOT_RETENTION_SECONDS` | 900 seconds for prior generations |
| `SNAPSHOT_MAX_BYTES` | 1 GiB retained serialized document budget; active generation must fit; older snapshots may expire early |
| `WRITE_ENABLED`, `WRITE_DEFAULT_MODE`, `YOLO_ENABLED` | Must remain false/review/false; enabling write is rejected in 0.1.1 |
| `TUNNEL_IMAGE` | Compose-only official image pinned by digest in the example |
| `OPENAI_TUNNEL_ID`, `OPENAI_TUNNEL_API_KEY` | Compose-only tunnel credentials, passed only to the tunnel service |

For GitHub, the default API root is api.github.com or enterprise `/api/v3`; for
GitLab `/api/v4`; for Forgejo `/api/v1`. M1 does not call forge APIs or verify CR
capabilities. Complex subpath installations should supply explicit API/repository
IDs; automatic owner/repo recognition is tested for ordinary URLs. Git credentials
are never inferred from the OAuth login.

To use `_FILE` secrets, override the sample MCP environment to remove its direct
secret interpolation and add the `_FILE` path with a read-only secret mount. Do
not set both forms. To use a private CA, mount it read-only and pass `GIT_CA_BUNDLE`
in an override; do not modify the tunnel environment. Keep `.env`, both persistent
directories and mounted secrets out of source control. Do not share expanded
`docker compose config` output containing real credentials.
