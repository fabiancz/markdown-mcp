import hashlib
import io
import json
import socket
from contextlib import contextmanager

import pytest
import yaml
from fastmcp import Client
from pydantic import ValidationError
from test_write import ACTOR, draft, queue
from test_write import writes as writes

from obsidian_mcp.config import Settings
from obsidian_mcp.models import DomainError, Principal
from obsidian_mcp.server import create_server
from obsidian_mcp.uploads import ChatFile, download_file, validate_download_url
from obsidian_mcp.write import Writer

FILE = ChatFile(
    download_url="https://files.oaiusercontent.com/file?secret=signed", file_id="file-1"
)


@contextmanager
def rejects(code):
    with pytest.raises(DomainError) as error:
        yield error
    assert error.value.code == code


@pytest.fixture
def download(monkeypatch):
    """Run the real HTTP parser/body reader against a synthetic TLS byte stream."""
    state = {"wire": b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabc", "connects": []}

    class Socket:
        def settimeout(self, timeout):
            assert 0 < timeout <= 5

        def connect(self, address):
            state["connects"].append(address)

        def sendall(self, data):
            state.setdefault("request", bytearray()).extend(data)

        def makefile(self, *args):
            return io.BytesIO(state["wire"])

        def close(self):
            pass

    class TLS:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == "files.oaiusercontent.com"
            return sock

    monkeypatch.setattr(
        "obsidian_mcp.uploads.socket.getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
    )
    monkeypatch.setattr("obsidian_mcp.uploads.socket.socket", lambda *a, **kw: Socket())
    monkeypatch.setattr("obsidian_mcp.uploads.ssl.create_default_context", lambda: TLS())
    return state


def wire(raw, *, headers=b""):
    return (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(len(raw)).encode()
        + b"\r\n"
        + headers
        + b"\r\n"
        + raw
    )


@pytest.mark.parametrize("length", [1999999, 2000000, 2000001])
@pytest.mark.parametrize("framing", ["length", "chunked", "close"])
def test_download_boundary(vault, download, length, framing):
    settings = vault[0]
    raw = b"\x00\xff" * (length // 2) + b"x" * (length % 2)
    if framing == "length":
        download["wire"] = wire(raw)
    elif framing == "chunked":
        download["wire"] = (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + format(length, "x").encode()
            + b"\r\n"
            + raw
            + b"\r\n0\r\n\r\n"
        )
    else:
        download["wire"] = b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + raw
    if length > settings.upload_max_file_bytes:
        with rejects("UPLOAD_TOO_LARGE"):
            download_file(FILE, settings)
    else:
        assert download_file(FILE, settings) == raw
    assert download["connects"] == [("8.8.8.8", 443)]
    request = download["request"]
    assert b"Host: files.oaiusercontent.com" in request
    assert b"Authorization" not in request and b"Cookie" not in request


@pytest.mark.parametrize(
    "url",
    [
        "http://files.oaiusercontent.com/f",
        "https://evil.example/f",
        "https://files.oaiusercontent.com.evil.example/f",
        "https://files.oaiusercontent.com:8443/f",
        "https://user:secret@files.oaiusercontent.com/f",
        "https://files.oaiusercontent.com/f#fragment",
        "file:///etc/passwd",
        "sandbox:/mnt/data/test.pdf",
        "https://127.0.0.1/f",
        "https://files.oaiusercontent.com/f\r\nInjected: yes",
        "https://files.oaiusercontent.com\\evil/f",
    ],
)
def test_download_denies_invalid_sources_before_network(vault, download, url):
    with rejects("UPLOAD_SOURCE_DENIED"):
        download_file(ChatFile(download_url=url, file_id="f"), vault[0])
    assert not download["connects"]


@pytest.mark.parametrize(
    "url,reason,host",
    [
        ("file_secret", "FILE_REFERENCE_NOT_RESOLVED", None),
        ("file-secret", "FILE_REFERENCE_NOT_RESOLVED", None),
        ("sediment://secret/file", "FILE_REFERENCE_NOT_RESOLVED", None),
        ("sandbox:/mnt/data/secret.png", "FILE_REFERENCE_NOT_RESOLVED", None),
        ("/mnt/data/secret.png", "LOCAL_PATH_OR_MISSING_SCHEME", None),
        ("file:///secret.png", "LOCAL_PATH_OR_MISSING_SCHEME", None),
        ("http://files.oaiusercontent.com/secret", "HTTPS_REQUIRED", None),
        ("https:///secret", "MISSING_HOST", None),
        ("https://[secret", "INVALID_URL", None),
        ("https://files.oaiusercontent.com:secret/file", "INVALID_URL", None),
        ("https://files.oaiusercontent.com:8443/secret", "PORT_NOT_ALLOWED", None),
        ("https://user:secret@files.oaiusercontent.com/f", "URL_CREDENTIALS_NOT_ALLOWED", None),
        ("https://@files.oaiusercontent.com/secret", "URL_CREDENTIALS_NOT_ALLOWED", None),
        ("https://files.oaiusercontent.com/f#secret", "URL_FRAGMENT_NOT_ALLOWED", None),
        ("https://files.oaiusercontent.com/secret\n", "INVALID_URL_CHARACTERS", None),
        ("https://files.oaiusercontent.com\\secret", "INVALID_URL_CHARACTERS", None),
        ("https://cdn.example.com/secret?sig=secret", "HOST_NOT_ALLOWED", "cdn.example.com"),
        ("https://%73ecret/file", "HOST_NOT_ALLOWED", None),
    ],
)
def test_source_diagnostics_are_specific_redacted_and_offline(
    vault, monkeypatch, url, reason, host
):
    def no_dns(*args, **kwargs):
        pytest.fail("Rejected file references must not reach DNS")

    monkeypatch.setattr("obsidian_mcp.uploads.socket.getaddrinfo", no_dns)
    with rejects("UPLOAD_SOURCE_DENIED") as error:
        download_file(ChatFile(download_url=url, file_id="secret-id"), vault[0])
    result = error.value.as_dict()
    assert result["details"] == {"reason": reason, **({"host": host} if host else {})}
    assert not result["retryable"]
    assert "secret" not in json.dumps(result)
    assert "secret" not in str(error.value)


async def test_mcp_returns_source_diagnostics_without_staging(writes, monkeypatch):
    settings, reader, writer, *_ = writes

    def no_dns(*args, **kwargs):
        pytest.fail("Internal file references must not reach DNS")

    monkeypatch.setattr("obsidian_mcp.uploads.socket.getaddrinfo", no_dns)
    async with Client(create_server(settings, reader)) as client:
        result = await client.call_tool(
            "upload_attachment",
            {
                "file": {"download_url": "sediment://secret", "file_id": "secret-id"},
                "idempotency_key": "diagnostic",
            },
            raise_on_error=False,
        )
    assert result.is_error
    payload = json.loads(result.content[0].text)
    assert payload["code"] == "UPLOAD_SOURCE_DENIED"
    assert payload["details"] == {"reason": "FILE_REFERENCE_NOT_RESOLVED"}
    assert "secret" not in result.content[0].text
    assert not writer.store.all() and not writer.adapter.prs


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "224.0.0.1"]
)
@pytest.mark.parametrize("allowed_hosts", ["files.oaiusercontent.com", "*", "*.oaiusercontent.com"])
def test_download_denies_private_and_mixed_dns(
    vault, download, monkeypatch, address, allowed_hosts
):
    settings = Settings(**dict(vault[0].model_dump(), upload_allowed_hosts=allowed_hosts))
    monkeypatch.setattr(
        "obsidian_mcp.uploads.socket.getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
        ],
    )
    with rejects("UPLOAD_SOURCE_DENIED") as error:
        download_file(FILE, settings)
    assert error.value.as_dict()["details"] == {"reason": "NON_PUBLIC_ADDRESS"}
    assert not download["connects"]


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/secret\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nshort",
        b"HTTP/1.1 200 OK\r\nContent-Length: invalid\r\n\r\n",
        wire(b"encoded", headers=b"Content-Encoding: gzip\r\n"),
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n8\r\nshort",
        wire(b"abc", headers=b"Transfer-Encoding: chunked\r\n"),
    ],
)
@pytest.mark.parametrize("allowed_hosts", ["files.oaiusercontent.com", "*"])
def test_download_errors_never_expose_url(vault, download, response, allowed_hosts):
    download["wire"] = response
    settings = Settings(**dict(vault[0].model_dump(), upload_allowed_hosts=allowed_hosts))
    with rejects("UPLOAD_DOWNLOAD_FAILED") as error:
        download_file(FILE, settings)
    assert "secret" not in str(error.value) and "signed" not in str(error.value)
    assert len(download["connects"]) == 1


@pytest.mark.parametrize(
    "pattern,host,allowed",
    [
        ("*", "any.example.net", True),
        ("*", "127.0.0.1", True),  # DNS/IP validation still rejects this before connecting.
        ("*.example.com", "cdn.example.com", True),
        ("*.example.com", "example.com", False),
        ("*.example.com", "a.cdn.example.com", False),
        ("*.example.com", "cdn.example.com.evil.net", False),
        ("*.example.com", "cdn.evil-example.com", False),
        ("oaisdmntpr*.blob.core.windows.net", "oaisdmntprukwest.blob.core.windows.net", True),
        ("oaisdmntpr*.blob.core.windows.net", "oaisdmntprdenmarkeast.blob.core.windows.net", True),
        ("oaisdmntpr*.blob.core.windows.net", "other.blob.core.windows.net", False),
        ("oaisdmntpr*.blob.core.windows.net", "oaisdmntpr.evil.blob.core.windows.net", False),
        ("oaisdmntpr*.blob.core.windows.net", "oaisdmntprukwest.blob.core.windows.net.evil", False),
        (
            " FILES.OAIUSERCONTENT.COM , oaisdmntpr*.blob.core.windows.net ",
            "files.oaiusercontent.com",
            True,
        ),
        ("FILES.OAIUSERCONTENT.COM", "FILES.OAIUSERCONTENT.COM", True),
        ("files.oaiusercontent.com", "other.oaiusercontent.com", False),
    ],
)
def test_upload_host_patterns(vault, pattern, host, allowed):
    settings = Settings(**dict(vault[0].model_dump(), upload_allowed_hosts=pattern))
    assert settings.allows_upload_host(host) is allowed
    if allowed:
        assert validate_download_url("https://" + host + "/file", settings).hostname == host.lower()
    else:
        with rejects("UPLOAD_SOURCE_DENIED") as error:
            validate_download_url("https://" + host + "/file", settings)
        assert error.value.as_dict()["details"]["reason"] == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize("pattern", ["*", "*.oaiusercontent.com", "files*.oaiusercontent.com"])
def test_download_through_wildcard_keeps_pinned_tls(vault, download, pattern):
    settings = Settings(**dict(vault[0].model_dump(), upload_allowed_hosts=pattern))
    assert download_file(FILE, settings) == b"abc"
    assert download["connects"] == [("8.8.8.8", 443)]
    assert b"Host: files.oaiusercontent.com" in download["request"]


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/f",
        "sediment://file",
        "sandbox:/mnt/data/f",
        "file:///f",
        "https://example.com:8443/f",
        "https://user:secret@example.com/f",
        "https://example.com/f#secret",
        "https://example.com/secret\n",
    ],
)
def test_global_wildcard_keeps_url_restrictions(vault, download, url):
    settings = Settings(**dict(vault[0].model_dump(), upload_allowed_hosts="*"))
    with rejects("UPLOAD_SOURCE_DENIED"):
        download_file(ChatFile(download_url=url, file_id="f"), settings)
    assert not download["connects"]


@pytest.mark.parametrize(
    "pattern",
    [
        "https://*.example.com",
        "*.example.com:443",
        "*.example.com/path",
        "**.example.com",
        "*.example..com",
        "?.example.com",
        "[a-z].example.com",
        "*.example.com#fragment",
        "user@*.example.com",
        "-*.example.com",
        "*.example.com.",
        "a" * 64 + ".example.com",
    ],
)
def test_upload_host_patterns_reject_invalid_configuration(vault, pattern):
    with pytest.raises(ValidationError, match="UPLOAD_ALLOWED_HOSTS"):
        Settings(**dict(vault[0].model_dump(), upload_allowed_hosts=pattern))


def test_configuration_and_compose(vault):
    from pathlib import Path

    settings = vault[0]
    for value in [0, -1, 1.2, True, "2MB", ""]:
        with pytest.raises(ValidationError):
            Settings(**dict(settings.model_dump(), upload_max_file_bytes=value))
    for size in [1, 3000000]:
        configured = Settings(**dict(settings.model_dump(), upload_max_file_bytes=size))
        assert configured.upload_max_file_bytes == size
    for update in [
        {"upload_allowed_hosts": "https://*.example.com"},
        {"upload_allowed_hosts": ""},
        {"upload_max_change_bytes": 1999999},
        {"attachments_folder": "../secret"},
        {"attachments_folder": ".git/objects"},
        {"attachments_folder": "attachments/"},
    ]:
        with pytest.raises((ValidationError, DomainError)):
            Settings(**dict(settings.model_dump(), **update))
    for profile in ["oauth", "tunnel"]:
        directory = Path(__file__).parents[1] / "examples" / profile
        env = yaml.safe_load((directory / "docker-compose.yaml").read_text())["services"]["mcp"][
            "environment"
        ]
        example = (directory / ".env.example").read_text()
        for field in [
            "upload_max_file_bytes",
            "upload_max_change_bytes",
            "upload_staging_max_bytes",
            "upload_retention_seconds",
            "upload_allowed_hosts",
            "attachments_folder",
        ]:
            name, value = field.upper(), str(getattr(settings, field))
            assert env[name] == "${" + name + ":-" + value + "}"
            assert name + "=" + value in example


async def staged(writer, monkeypatch, raw=b"\x00\xffattachment", key="upload"):
    monkeypatch.setattr("obsidian_mcp.uploads.download_file", lambda *a: raw)
    return (await writer.upload_attachment(FILE, key, actor=ACTOR))["data"]


def operation(upload, path="attachments/photo.png"):
    return {"op": "create_attachment", "path": path, "upload_id": upload["upload_id"]}


async def test_staging_retry_ownership_expiry_quota_and_revocation(writes, monkeypatch):
    settings, _, writer, *_ = writes
    await writer.reader.snapshot()
    first = await staged(writer, monkeypatch)
    assert await staged(writer, monkeypatch) == first
    with rejects("IDEMPOTENCY_CONFLICT"):
        await staged(writer, monkeypatch, b"different")
    stranger = Principal(subject="someone-else", roles=frozenset({"reader", "writer"}))
    with rejects("PERMISSION_DENIED"):
        await writer.upload_attachment(FILE, "x", actor=stranger)
    with rejects("UPLOAD_NOT_FOUND"):
        writer.uploads.read(first["upload_id"], stranger.subject)
    reader = Principal(subject=ACTOR.subject, roles=frozenset({"reader"}))
    with rejects("PERMISSION_DENIED"):
        await writer.upload_attachment(FILE, "x", actor=reader)
    settings.upload_staging_max_bytes = first["size_bytes"]
    with rejects("UPLOAD_QUOTA"):
        await staged(writer, monkeypatch, key="second")
    with writer.store.connect() as db:
        db.execute("UPDATE uploads SET expires=0")
    writer.uploads.cleanup()
    with rejects("UPLOAD_NOT_FOUND"):
        writer.uploads.read(first["upload_id"], ACTOR.subject)
    settings.write_enabled = False
    with rejects("WRITE_DISABLED"):
        await writer.upload_attachment(FILE, "x", actor=ACTOR)


async def test_atomic_max_upload_survives_staging_expiry_restart_and_gc(writes, monkeypatch):
    settings, reader, writer, remote, *_ = writes
    await reader.snapshot()
    raw = bytes(range(256)) * 7812 + bytes(range(128))  # exactly 2 MB
    assert len(raw) == 2000000
    upload = await staged(writer, monkeypatch, raw)
    prepared = await draft(
        writer,
        operations=[
            operation(upload),
            {
                "op": "create",
                "path": "notes/Photo.md",
                "content": "# Photo\n![[attachments/photo.png]]\n",
            },
        ],
    )
    assert prepared["attachments"] == [
        {
            "path": "attachments/photo.png",
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    ]
    assert len(prepared["diff"]) < 2000 and "GIT binary patch" not in prepared["diff"]
    assert "notes/Photo.md" in prepared["diff"]
    with writer.store.connect() as db:
        db.execute("UPDATE uploads SET expires=0")
    writer.uploads.cleanup()
    with writer.source.lock():
        writer.workspace.run(["gc", "--prune=now"])
    restarted = Writer(settings, reader, writer.adapter)
    await queue(restarted, prepared)
    restarted.adapter.fail_create = True
    await restarted.run_once()
    change = restarted.store.get(prepared["change_id"])
    change["next_attempt"] = 0
    restarted.store.save(change, "test_retry")
    restarted = Writer(settings, reader, writer.adapter)
    await restarted.run_once()
    result = restarted.store.get(prepared["change_id"])
    assert result["status"] == "awaiting_review" and len(writer.adapter.prs) == 1
    with writer.source.lock():
        assert (
            writer.workspace.run(["show", result["head_commit"] + ":attachments/photo.png"]) == raw
        )
    assert (
        settings.repo_root / "worktrees" / prepared["change_id"] / "attachments/photo.png"
    ).read_bytes() == raw
    assert all(
        item["path"] != "notes/Photo.md" for item in (await reader.list_notes())["data"]["items"]
    )


@pytest.mark.parametrize(
    "path",
    [
        "../escape.png",
        "attachments/../escape.png",
        ".git/test",
        "notes/photo.png",
        "attachments/.gitattributes",
        "attachments/note.md",
        "attachments/.obsidian/x",
        "attachments/x/../y",
    ],
)
async def test_attachment_path_policy_is_atomic(writes, monkeypatch, path):
    writer = writes[2]
    await writer.reader.snapshot()
    upload = await staged(writer, monkeypatch)
    with pytest.raises(DomainError):
        await draft(
            writer,
            operations=[
                operation(upload, path),
                {"op": "create", "path": "notes/OK.md", "content": "# OK"},
            ],
        )
    assert not writer.store.all() and not writer.adapter.prs


async def test_foreign_handle_collisions_and_changed_policy(writes, monkeypatch):
    settings, reader, writer, remote, origin, _ = writes
    await reader.snapshot()
    upload = await staged(writer, monkeypatch)
    with rejects("UPLOAD_NOT_FOUND"):
        await draft(writer, operations=[operation({"upload_id": "upl_absent"})])
    with rejects("PATH_COLLISION"):
        await draft(
            writer,
            operations=[
                operation(upload, "attachments/folder"),
                operation(upload, "attachments/folder/file"),
            ],
        )
    prepared = await draft(writer, operations=[operation(upload)])
    settings.upload_max_file_bytes = 1
    await queue(writer, prepared)
    await writer.run_once()
    assert writer.store.get(prepared["change_id"])["code"] == "POLICY_CHANGED"
    assert not writer.adapter.prs


async def test_mcp_file_schema_and_stage_prepare(writes, monkeypatch):
    settings, reader, writer, *_ = writes
    monkeypatch.setattr("obsidian_mcp.uploads.download_file", lambda *a: b"\x00\xfftest")
    server = create_server(settings, reader)
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        tool = tools["upload_attachment"]
        assert tool.meta["openai/fileParams"] == ["file"]
        schema = tool.inputSchema
        file_schema = schema["properties"]["file"]
        if "$ref" in file_schema:
            file_schema = schema["$defs"][file_schema["$ref"].split("/")[-1]]
        assert set(file_schema["required"]) == {"download_url", "file_id"}
        assert {k: v["type"] for k, v in file_schema["properties"].items()} == {
            "download_url": "string",
            "file_id": "string",
            "mime_type": "string",
            "file_name": "string",
        }
        assert not tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
        response = (
            await client.call_tool(
                "upload_attachment", {"file": FILE.model_dump(), "idempotency_key": "mcp"}
            )
        ).data
        status = (await client.call_tool("get_vault_status", {})).data
        result = (
            await client.call_tool(
                "prepare_change",
                {
                    "operations": [operation(response["data"])],
                    "summary": "Add photo",
                    "idempotency_key": "photo",
                    "base_snapshot_id": status["meta"]["snapshot_id"],
                },
            )
        ).data
        assert result["data"]["status"] == "prepared"
        assert "signed" not in str(response) and "download_url" not in str(response)


async def test_total_budget_corruption_cancel_and_target_collision(writes, monkeypatch):
    from conftest import git

    settings, reader, writer, remote, origin, _ = writes
    await reader.snapshot()
    upload = await staged(writer, monkeypatch, b"data")
    settings.upload_max_change_bytes = 7
    with rejects("UPLOAD_TOO_LARGE"):
        await draft(
            writer, operations=[operation(upload), operation(upload, "attachments/other.bin")]
        )
    settings.upload_max_change_bytes = 10000000
    with writer.store.connect() as db:
        db.execute("UPDATE uploads SET content=?", (b"tamper",))
    with rejects("UPLOAD_CORRUPT"):
        await draft(writer, operations=[operation(upload)])
    with writer.store.connect() as db:
        db.execute("UPDATE uploads SET content=?", (b"data",))
    prepared = await draft(writer, operations=[operation(upload)])
    await writer.cancel_change(prepared["change_id"], actor=ACTOR)
    with rejects("CHANGE_STATE"):
        await queue(writer, prepared)
    prepared = await draft(writer, key="collision", operations=[operation(upload)])
    (origin / "attachments").mkdir()
    (origin / "attachments/photo.png").write_bytes(b"someone else's file")
    git(origin, "add", "attachments/photo.png")
    git(origin, "commit", "-m", "test: concurrent attachment")
    git(origin, "push", str(remote), "main")
    await queue(writer, prepared)
    await writer.run_once()
    change = writer.store.get(prepared["change_id"])
    assert change["code"] == "PATH_COLLISION" and not writer.adapter.prs
    assert (origin / "attachments/photo.png").read_bytes() == b"someone else's file"


async def test_upload_revoked_during_download_and_failed_download_is_not_staged(
    writes, monkeypatch
):
    settings, reader, writer, *_ = writes
    await reader.snapshot()

    def revoke(*args):
        settings.write_enabled = False
        return b"file"

    monkeypatch.setattr("obsidian_mcp.uploads.download_file", revoke)
    with rejects("WRITE_DISABLED"):
        await writer.upload_attachment(FILE, "revoked", actor=ACTOR)
    with writer.store.connect() as db:
        assert db.execute("SELECT count(*) FROM uploads").fetchone()[0] == 0
    settings.write_enabled = True

    def fail(*args):
        raise DomainError("UPLOAD_DOWNLOAD_FAILED", "Synthetic failed transfer")

    monkeypatch.setattr("obsidian_mcp.uploads.download_file", fail)
    with rejects("UPLOAD_DOWNLOAD_FAILED"):
        await writer.upload_attachment(FILE, "failed", actor=ACTOR)
    with writer.store.connect() as db:
        assert db.execute("SELECT count(*) FROM uploads").fetchone()[0] == 0


async def test_concurrent_upload_replay_and_payload_integrity(writes, monkeypatch):
    import asyncio

    writer = writes[2]
    await writer.reader.snapshot()
    monkeypatch.setattr("obsidian_mcp.uploads.download_file", lambda *args: b"bytes")
    results = await asyncio.gather(
        *[writer.upload_attachment(FILE, "concurrent", actor=ACTOR) for _ in range(3)]
    )
    assert len({r["data"]["upload_id"] for r in results}) == 1
    prepared = await draft(writer, operations=[operation(results[0]["data"])])
    change = writer.store.get(prepared["change_id"])
    with writer.source.lock():
        oid = (
            writer.workspace.run(["hash-object", "-w", "--stdin"], input=b"tampered")
            .decode()
            .strip()
        )
        change["result_blobs"]["attachments/photo.png"] = ["100644", oid]
        with rejects("DIFF_MISMATCH"):
            writer.workspace.commit(change)


def test_download_custom_limit_timeout_and_tls_failure(vault, download, monkeypatch):
    settings = vault[0]
    settings.upload_max_file_bytes = 2
    with rejects("UPLOAD_TOO_LARGE"):
        download_file(FILE, settings)
    settings.upload_max_file_bytes = 3000000
    raw = b"x" * 2000001
    download["wire"] = wire(raw)
    assert download_file(FILE, settings) == raw
    ticks = iter([0, 21])
    monkeypatch.setattr("obsidian_mcp.uploads.time.monotonic", lambda: next(ticks))
    with rejects("UPLOAD_DOWNLOAD_FAILED"):
        download_file(FILE, settings)


def test_real_https_stream_certificate_and_redirect(vault, tmp_path, monkeypatch):
    import ssl
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    settings = vault[0]
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=files.oaiusercontent.com",
            "-addext",
            "subjectAltName=DNS:files.oaiusercontent.com",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    raw = bytes(range(256)) * 7812 + bytes(range(128))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            self.send_response(302 if self.path == "/redirect" else 200)
            self.send_header("Content-Length", "0" if self.path == "/redirect" else str(len(raw)))
            self.send_header("Connection", "close")
            if self.path == "/redirect":
                self.send_header("Location", "https://files.oaiusercontent.com/other")
            self.end_headers()
            if self.path != "/redirect":
                self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_socket = socket.socket
    original_dns = socket.getaddrinfo
    original_context = ssl.create_default_context

    class RoutedSocket(original_socket):
        def connect(self, address):
            # Substitute only the network destination for this disposable local TLS fixture.
            if address == ("8.8.8.8", 443):
                address = server.server_address
            return super().connect(address)

    def resolve(host, *args, **kwargs):
        if host == "files.oaiusercontent.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        return original_dns(host, *args, **kwargs)

    try:
        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(socket, "socket", RoutedSocket)
        monkeypatch.setattr(
            ssl, "create_default_context", lambda: original_context(cafile=str(cert))
        )
        assert download_file(FILE, settings) == raw
        assert requests[0][1]["Host"] == "files.oaiusercontent.com"
        assert "Authorization" not in requests[0][1]
        with rejects("UPLOAD_DOWNLOAD_FAILED"):
            download_file(
                ChatFile(download_url="https://files.oaiusercontent.com/redirect", file_id="f"),
                settings,
            )
        assert len(requests) == 2
        monkeypatch.setattr(ssl, "create_default_context", original_context)
        with rejects("UPLOAD_DOWNLOAD_FAILED"):
            download_file(FILE, settings)
        assert len(requests) == 2  # Rejected before HTTP: the fixture CA is no longer trusted.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
