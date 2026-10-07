# Attachments from an AI chat

Version 0.3.0rc1 adds `upload_attachment(file, idempotency_key)` and the
`create_attachment` change operation. Published 0.2.3 images do not include it. After the candidate publishing workflow
succeeds, use `ghcr.io/fabiancz/markdown-mcp:0.3.0rc1` for acceptance testing;
otherwise build this checkout locally.

The tool declares `_meta["openai/fileParams"] = ["file"]`, following the
[OpenAI file input contract](https://developers.openai.com/plugins/reference#file-apis)
checked on October 7, 2026. The client supplies a required `download_url` and
`file_id`, with optional string `file_name` and `mime_type`. The server downloads
the bytes from that temporary URL. No widget, new inbound upload route or OpenAI
API key is needed by this implementation. The existing authenticated `/mcp`
transport carries the file reference for both deployment profiles.

Actual ChatGPT delivery of user-attached and generated files, including delivery
over Secure MCP Tunnel, still needs operator acceptance. Documentation and local
MCP tests alone do not certify those client capabilities. Other clients can use
the same descriptor if they can supply a file URL on an operator-approved host;
there is no local-path, arbitrary URL import or Base64 fallback.

## Configuration

Set these values in the deployment `.env` and use the updated Compose example,
which forwards them to the MCP container:

```dotenv
UPLOAD_MAX_FILE_BYTES=2000000
UPLOAD_MAX_CHANGE_BYTES=10000000
UPLOAD_STAGING_MAX_BYTES=100000000
UPLOAD_RETENTION_SECONDS=86400
UPLOAD_ALLOWED_HOSTS=files.oaiusercontent.com
ATTACHMENTS_FOLDER=attachments
```

| Setting | Meaning |
| --- | --- |
| `UPLOAD_MAX_FILE_BYTES` | Maximum original bytes per file, inclusive; default 2 MB = 2,000,000 bytes |
| `UPLOAD_MAX_CHANGE_BYTES` | Maximum total attachment bytes in one proposal, default 10 MB |
| `UPLOAD_STAGING_MAX_BYTES` | Maximum bytes in transient staging, default 100 MB |
| `UPLOAD_RETENTION_SECONDS` | Lifetime of an upload handle, default one day; retries do not extend it |
| `UPLOAD_ALLOWED_HOSTS` | Comma-separated exact download hostnames, without schemes, ports or wildcards |
| `ATTACHMENTS_FOLDER` | Relative vault directory without a trailing slash; default `attachments` |

Upload limits must be positive integers. The change and staging budgets must
each accommodate one maximum-size file. Increasing the per-file limit beyond
those budgets requires increasing the corresponding budget as well. There is
also a fixed cap of 1,000 staged handles; empty files count toward it. Concurrent
downloads are serialized. Staging limits do not cap Git history or prepared
draft storage. SQLite may retain freed disk pages for reuse.

`WRITE_ENABLED=true` and current write permission are required. Uploads use the
same identity and repository binding as proposals. `ALLOWED_FOLDERS` and
`EXCLUDED_FOLDERS` also apply: include the attachment directory in your allowlist
when one is configured. Uploads cannot write `.git*`, `.obsidian`, `.trash`,
symlinks, submodules, occupied paths or colliding case/Unicode names. Markdown
files use the existing note operations so attachment uploads cannot bypass
Markdown validation. Existing attachments cannot be replaced, renamed or deleted
through this first upload implementation.

The default download hostname is an initial restrictive configuration, not a
guarantee that every ChatGPT file uses that hostname. If a real file is rejected
with `UPLOAD_SOURCE_DENIED`, inspect only its hostname, verify the file source,
and add that exact host to the operator configuration if appropriate. Do not
share signed URL query strings. Redirects are rejected even between allowed
hosts. The MCP container needs outbound HTTPS access to the download host;
`GIT_ALLOW_HTTP` does not weaken upload rules.

Download connections pin an already validated public IP while verifying TLS
against the original hostname. Private/link-local/loopback/mixed DNS answers,
credentials in URLs, non-443 ports and compressed responses are rejected. Vault
PATs, OAuth credentials, cookies and environment proxies are never forwarded.
The downloader uses a five-second DNS/socket timeout and a 20-second transfer
deadline; an expired or failed reference requires a fresh client reference.
Signed URLs are neither persisted nor included in returned errors.

The MCP request contains small file metadata, rather than the file body. The
file limit is independent of `MAX_FILE_BYTES` (Markdown), `WRITE_MAX_BYTES`
(normalized operations/preview), and `MAX_RESPONSE_BYTES`. Keep ordinary
request-size and timeout controls at your proxy. Raising proxy body limits to
2 MB is unnecessary for this transport; client/tunnel timeouts must allow the
download and tool response. No inbound file endpoint is exposed.

## Workflow and guarantees

1. Ask the AI client to pass the actual attached or generated file to
   `upload_attachment`. Do not invent a URL or pass `sandbox:/...` or `/mnt/data/...`.
2. The response returns `upload_id`, `sha256`, `size_bytes`, `expires_at` (Unix
   seconds), `attachments_folder`, `max_file_bytes` and `status=staged`.
   The bytes are durable in `data/state.sqlite`; the vault has not changed.
3. Read the vault snapshot, then prepare one change containing the attachment
   operation and any note/link operations, for example:

```json
[
  {"op":"create_attachment","path":"attachments/photo.png","upload_id":"upl_..."},
  {"op":"create","path":"Trips/Photo.md","content":"# Photo\n![[attachments/photo.png]]\n"}
]
```

4. Inspect the compact preview. It contains the Markdown diff and an attachment
   manifest of paths, sizes and SHA-256 hashes. `diff_hash` binds both. Binary
   contents are not returned as a patch or encoded text.
5. Submit that exact hash and follow the existing review PR workflow. These
   operations also work in `prepare_change_update` for the same open PR.
   Staged, prepared, PR-open and merged remain separate states.
6. After manual merge, `get_change` on the original change reports target
   visibility. `attachments_verified=true` means the current target has the
   expected bytes for the original proposal and successfully applied updates.
   If manual review or a later commit changed/removed them, it reports `false`
   with a warning while preserving the fact that the PR merged. A null field
   means no attachment verification has been recorded. Binary content is not
   searchable through FTS or readable through `read_note`.

Repeating an upload key re-downloads a fresh reference and compares the file ID
and SHA-256; different content returns `IDEMPOTENCY_CONFLICT`. Upload idempotency
lasts until handle expiry; after expiry re-upload and use the new handle in a
new proposal. Proposal idempotency remains durable. Prepared proposals pin their
Git trees before journaling, so upload expiry, restart and Git garbage collection
cannot remove the bytes they reference. Automatic staging cleanup runs during
upload and worker ticks; it does not delete prepared trees or journal records.
Back up both mounts consistently. Cancellation does not undo a published PR.

Errors include `UPLOAD_TOO_LARGE`, `UPLOAD_SOURCE_DENIED`,
`UPLOAD_DOWNLOAD_FAILED`, `UPLOAD_QUOTA`, `UPLOAD_NOT_FOUND` and `UPLOAD_CORRUPT`,
alongside existing authorization, path, policy and idempotency errors. Download
errors do not publish a partial file or proposal.

## Operator acceptance in the actual AI client

Use a test vault/repository and a build containing this change. Preserve both
mounts through container recreation and refresh the client's tool discovery.
There should be seventeen tools with write enabled, including `upload_attachment`.

1. Attach a small PNG or PDF to a new chat and ask the AI to upload it, create
   a linked note and prepare one review PR. Confirm that the tool receives a
   file object and the response says `staged`. A plain filename is insufficient.
2. In a second chat ask the AI to generate a small CSV or image, then perform
   the same workflow using the generated file.
3. Review the PR, merge manually, synchronize Obsidian and open the file/link.
   Compare downloaded bytes/SHA-256 with the upload response. Confirm the
   original `get_change` reports `merged=true` and `attachments_verified=true`.
4. Try a file of 2,000,001 bytes with the default configuration: it must return
   `UPLOAD_TOO_LARGE`, with no partial vault file. A 2,000,000-byte file must fit.
5. Record client/version, OAuth or tunnel profile, and success for both file
   origins separately. If file delivery fails, report the error code and host
   only, without PATs, signed URL parameters or private file contents.
