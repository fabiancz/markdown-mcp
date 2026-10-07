# Deploy, verify and recover

## OAuth over HTTPS

Use `examples/oauth/` with an HTTPS reverse proxy. The MCP host port binds only
to `127.0.0.1`. Forward all paths, including discovery, consent, client registration,
`/token`, `/auth/callback` and `/mcp`; preserve Authorization and disable buffering.
An nginx location example is in `examples/nginx/mcp.conf`. Certificates and DNS
are supplied by the operator. Set `PUBLIC_BASE_URL` to the exact public HTTPS origin.

Create a GitHub OAuth App with `/auth/callback` on that origin. Set the Client ID
and Client Secret separately from `GIT_PAT`. Add permitted numeric GitHub user
IDs in `GITHUB_ALLOWED_USER_IDS`. Scope is `read:user`; local authorization grants
read, with optional write for the explicit `GITHUB_WRITE_USER_IDS` subset. Provide the exact HTTPS MCP-client redirects in `OAUTH_REDIRECT_URIS`.
Unknown users, missing configuration and invalid tokens are denied. CIMD is
currently disabled; dynamic registration is supported with the configured redirects.
Consent remains enabled. No client-controlled Host or forwarded header constructs
the advertised OAuth URLs.

OAuth state, encrypted upstream credentials and generated signing/storage keys
live in `data/auth/`. Preserve the whole directory across recreation and upgrades.
GitHub OAuth App responses without an upstream refresh token also have no MCP
refresh token. They remain subject to expiry and live upstream validation; on
expiry or GitHub revocation reconnect the client and repeat login/consent. The
library's refresh path is tested with synthetic refresh-capable upstream responses,
which does not assert GitHub OAuth Apps issue refresh tokens.

## Secure MCP Tunnel (experimental)

Use `examples/tunnel/`. It has two services, no published ports and the fixed
internal target `http://mcp:8000/mcp`. Configure tunnel access in the intended
OpenAI workspace and give only intended users access. Everyone using that tunnel
has the same vault and the same local `tunnel_operator` rights (write too when enabled). It does not
identify individual ChatGPT users.

The tunnel service receives only its runtime credential and tunnel ID; it does
not receive the Git PAT or mount vault/state directories. The MCP service receives
no OpenAI runtime key. The official tunnel image is digest-pinned. Startup waits
for MCP health; restart policy supports process failure recovery. Actual tunnel
reconnection, access denial and ChatGPT read still require the P0-T deployment test.
Never expose this unauthenticated internal MCP profile on a public host port.

The upstream Docker deployment reference is
[the official tunnel-client guide](https://github.com/openai/tunnel-client/blob/master/docs/deployment/docker.md).

## Git and persistence

The app uses a credential-free HTTPS clone URL plus username/PAT through a scoped
Git credential helper. TLS verification remains on; redirects are rejected.
For a Git server on a trusted private network without HTTPS, set
`GIT_ALLOW_HTTP=true` and use a plain `http://HOST:PORT/owner/vault.git` URL.
HTTP sends the PAT and note content unencrypted. The container must be able to
reach that host and port; a LAN IP can be used directly. `localhost` inside the
container refers to the container itself. Both Compose examples pass this setting
to MCP only; OAuth's public origin still requires HTTPS. SSH is unsupported.
Use a service account with read access to the selected repository. M1 performs
clone/fetch only and requires no branch push/CR/merge rights. GitHub/GitLab/Forgejo
HTTPS URLs are configurable; vendor PAT/scope enforcement must be verified on your
instance. Forgejo review write uses the same PAT for Git and API; see [write setup](write.md).

`repo/checkout` is managed service data. Do not use it as an editable Obsidian
vault. Fetch reads the remote target commit without reset/pull or changing the
checked-out branch; uncommitted changes generate a warning and are preserved.
The application stages initial clone atomically inside `repo/`, detecting an
incorrect existing clone/source instead of overwriting it. The default branch is
discovered once and persisted. Run one active process per installation.

Startup checks mount writability. On Linux, initialize ownership with
`scripts/init-volumes.sh` (or the commands in README). For custom host locations,
set `REPO_DIR` and `DATA_DIR`; container paths stay `/repo` and `/data`.

## Health and acceptance

`/health/live` exposes only process liveness. `/health/ready` checks an available
snapshot without network fetch and returns 503 when unavailable. Authenticated
`get_vault_status` returns counts, freshness and diagnostics without fetch.
New data requests fetch by default; concurrent requests share the same job.
Offline reads visibly serve the last successful snapshot or fail under strict
policy. A failing first sync prevents ready startup.

Before accepting production M1, complete the following against your actual client:

1. OAuth discovery, allowed login, denied login, search and pinned read.
2. Recreate the container; verify stored auth works and same-content snapshot persists.
3. Revoke GitHub consent, confirm denial, then reconnect successfully.
4. For tunnel: no additional OAuth, authorized/unauthorized workspace access,
   restart/disconnection recovery and search/read matching revision.
5. Confirm PAT/TLS and private Git connectivity from the deployed container.

Local tests are evidence for implementation behavior, not a replacement for these
external checks. No service is automatically deployed by the source implementation.

## Upgrade and rebuild

Back up both mounts and `.env` while the service is stopped. SQLite WAL files must
be backed up consistently; copying a live DB alone is insufficient. Protect backups
like the original private vault. For a released version: pull the chosen image,
read migration notes, then recreate the service. Do not downgrade onto newer state.

Set `IMAGE` in `.env` to the full released reference, then run:

```sh
docker compose pull mcp
docker compose up -d mcp
```

Pulling an image alone does not recreate the running container. `.env` supplies
Compose substitutions; settings reach MCP only when the service's `environment`
section passes them through. Keep your Compose file in sync with the distributed
example, particularly `WRITE_ENABLED` and the OAuth writer allowlist.

After changing tools or configuration, refresh the connection in ChatGPT Plugins
and start a new conversation, as described in
[OpenAI's connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt).
A fresh read-only installation advertises seven tools; enabling Forgejo review
writes adds nine tools in 0.2.2, for sixteen total (twelve in 0.2.0/0.2.1).
Persisted write state also keeps those
tools registered for observation after disabling writes; mutations still enforce
the current policy. Use authenticated `ping` to check the running application's
`version`, `write_enabled` and `write_default_mode` (available since 0.2.1).
The MCP initialization `serverInfo.version` uses the installed package version,
which matches the release tag. Plugin information displayed by ChatGPT may have
separate metadata; use the MCP response to verify the running application.

The 0.3.0rc1 attachment candidate adds one more tool
(seventeen total with write enabled). Update both Compose and `.env` to forward
the upload limits, permitted download hosts and attachment directory. The MCP
container needs outbound HTTPS to those hosts, including with the tunnel profile.
There is no additional inbound endpoint. Follow the
[actual-client acceptance steps](attachments.md#operator-acceptance-in-the-actual-ai-client)
for attached and generated files separately.

The server advertises its bundled blue crystal icon in `serverInfo.icons` as an
embedded PNG, with no additional configuration or public asset hosting. After
upgrading, refresh the client connection to retrieve the metadata. Whether
ChatGPT uses this icon in its interface still requires a live client check.

To rebuild only the derived index: stop MCP, move `data/index.sqlite` and any
`index.sqlite-wal`/`index.sqlite-shm` aside, then start MCP. Keep `data/source.json`,
`data/auth/`, `data/state.sqlite` and all repo
data. Startup automatically quarantines a corrupt derived index and rebuilds it.
Index rebuild does not delete persistent identity/auth/job state. For a different
Git repository, use a new pair of mount directories; no implicit migration/reset.

Publication is a separate action. GitHub Actions builds/tests both architectures
and promotes the tested images to `ghcr.io/fabiancz/markdown-mcp`. Version tags or
manual dispatch on `main` publish stable releases; main pushes publish commit
images. See [release instructions](releases.md). License selection, package public
visibility, live ChatGPT acceptance and tunnel acceptance remain pending before
calling this a public production release.

## Forgejo review acceptance

Version 0.2.2 extends opt-in review writes with feedback reads and approved
commits to the same PR, locally verified on Forgejo 15.0.9. Use a regular service
account with repository write access and a PAT scoped to `write:repository` plus
`read:issue` for discussion reads. Add that scope when upgrading from 0.2.1 and
recreate the MCP service with the updated token. No new environment variable or
state migration is needed; preserve the existing mounts. Automatic merge,
approvals and protection bypass are unavailable. Other hosting write adapters
are not implemented. [Write contracts and recovery](write.md) describe setup,
permissions, cancellation and durable effects.

Before a production write, verify the deployed Git/API network path, actual
instance Swagger/version and PAT permissions on a separate synthetic repository.
Then complete your MCP client's prepare/submit/review/merge/read flow. Preserve
both mounts through recreation. Local integration tests do not certify your
instance, branch rules, ChatGPT connection or tunnel boundary.
