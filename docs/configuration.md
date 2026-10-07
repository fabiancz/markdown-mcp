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
| `GIT_REPO_URL` | Required credential-free HTTPS repository URL; HTTP requires `GIT_ALLOW_HTTP=true`; no URL username/PAT/query/fragment |
| `GIT_ALLOW_HTTP` | false; explicitly allow plain HTTP Git and forge API URLs on a trusted network; credentials and note content travel unencrypted |
| `GIT_TARGET_BRANCH` | Empty discovers/persists the remote default branch; explicit branch takes precedence |
| `GIT_USERNAME`, `GIT_PAT` | Required service-account HTTPS credentials; M1 requires repository read access only |
| `GIT_PAT_FILE` | Optional mounted secret file; conflicts with a nonempty `GIT_PAT` |
| `GIT_CA_BUNDLE` | Optional mounted CA certificate bundle for Git and Forgejo API; TLS verification cannot be disabled |
| `FORGE_REPO_ID` | Optional owner/repo or GitLab namespace/subgroup/project override |
| `FORGE_API_URL` | Optional HTTPS API root (HTTP requires `GIT_ALLOW_HTTP=true`), required for ambiguous subpath hosting |
| `GIT_COMMIT_NAME`, `GIT_COMMIT_EMAIL` | Service commit metadata, defaults Vault MCP Bot / vault-mcp@example.invalid |
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
| `WRITE_ENABLED` | false; opt-in Forgejo review writes; other write providers are rejected |
| `WRITE_DEFAULT_MODE`, `YOLO_ENABLED` | Must remain review/false; automatic merge is unavailable |
| `GITHUB_WRITE_USER_IDS` | Empty by default; OAuth writer IDs must be a subset of the read allowlist |
| `WRITE_MAX_OPERATIONS`, `WRITE_MAX_BYTES` | 50 operations and 1 MiB normalized request/diff; preview must also fit `MAX_RESPONSE_BYTES` |
| `UPLOAD_MAX_FILE_BYTES` | 2,000,000 original bytes per attachment, inclusive (2 MB); independent of Markdown limits |
| `UPLOAD_MAX_CHANGE_BYTES` | 10,000,000 total attachment bytes per proposal |
| `UPLOAD_STAGING_MAX_BYTES` | 100,000,000 transient upload bytes; also capped at 1,000 handles |
| `UPLOAD_RETENTION_SECONDS` | 86,400 seconds for staged handles; prepared Git trees survive expiry |
| `UPLOAD_ALLOWED_HOSTS` | `files.oaiusercontent.com`; comma-separated exact HTTPS download hosts; no wildcards/redirects/private IPs |
| `ATTACHMENTS_FOLDER` | `attachments`; relative vault directory, also subject to folder policy |
| `WRITE_POLL_SECONDS` | 10 seconds between background status checks; retries back off to 300 seconds |
| `FORGE_TIMEOUT_SECONDS` | 15 seconds per HTTP operation |
| `TUNNEL_IMAGE` | Compose-only official image pinned by digest in the example |
| `OPENAI_TUNNEL_ID`, `OPENAI_TUNNEL_API_KEY` | Compose-only tunnel credentials, passed only to the tunnel service |

Upload settings are new in 0.3.0rc1 and are not supported by published
0.2.3 images. Limits must be positive integers, and change/staging budgets must
each fit one maximum-size file. Read [attachments](attachments.md) for the
transport, hostname configuration and pending client acceptance.

For GitHub, the default API root is api.github.com or enterprise `/api/v3`; for
GitLab `/api/v4`; for Forgejo `/api/v1`. Read-only installations do not call forge APIs. Forgejo write verifies the
repository and review Swagger contract before publishing. Complex subpath installations should supply explicit API/repository
IDs; automatic owner/repo recognition is tested for ordinary URLs. Git credentials
are never inferred from the OAuth login.

Forgejo review writes require repository write access and a `write:repository`
PAT. Since 0.2.2, add `read:issue` to read PR discussion alongside reviews and
inline comments. Repository-specific tokens are supported; no admin/user scope
is needed. Use the same credential for Git and the API.

To use `_FILE` secrets, override the sample MCP environment to remove its direct
secret interpolation and add the `_FILE` path with a read-only secret mount. Do
not set both forms. To use a private CA, mount it read-only and pass `GIT_CA_BUNDLE`
in an override; do not modify the tunnel environment. Keep `.env`, both persistent
directories and mounted secrets out of source control. Do not share expanded
`docker compose config` output containing real credentials.
