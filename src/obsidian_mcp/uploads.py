"""Bounded file-param downloads and durable, identity-scoped attachment staging."""

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .models import DomainError
from .repo_lock import file_lock

_dns = ThreadPoolExecutor(max_workers=1, thread_name_prefix="upload-dns")


class ChatFile(BaseModel):
    """OpenAI fileParams schema; optional metadata must remain optional strings."""

    model_config = ConfigDict(extra="forbid", strict=True)
    download_url: str = Field(min_length=1, max_length=16384, repr=False)
    file_id: str = Field(min_length=1, max_length=512)
    mime_type: str = Field(default="", max_length=255)
    file_name: str = Field(default="", max_length=1024)


def download_file(file: ChatFile, settings) -> bytes:
    """Fetch only an allowed HTTPS host, with TLS verification and pinned public DNS."""
    try:
        url = urlsplit(file.download_url)
        if (
            url.scheme != "https"
            or url.hostname not in settings.upload_hosts
            or url.port not in (None, 443)
            or url.username
            or url.password
            or url.fragment
            or any(ord(c) <= 32 or ord(c) == 127 for c in file.download_url)
            or "\\" in file.download_url
        ):
            raise ValueError
    except ValueError:
        raise DomainError(
            "UPLOAD_SOURCE_DENIED",
            "File URL must use HTTPS on an exact UPLOAD_ALLOWED_HOSTS name, port 443, "
            "without credentials or a fragment. Local and sandbox paths cannot be uploaded.",
        ) from None
    conn = None
    raw_socket = None
    response = None
    watchdog = None
    deadline = time.monotonic() + 20

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return min(left, 5)

    try:
        lookup = _dns.submit(socket.getaddrinfo, url.hostname, 443, type=socket.SOCK_STREAM)
        try:
            addresses = lookup.result(timeout=remaining())
        finally:
            lookup.cancel()  # Do not accumulate queued lookups if the system resolver stalls.
        if not addresses or any(
            not ipaddress.ip_address(a[4][0]).is_global
            or ipaddress.ip_address(a[4][0]).is_multicast
            or getattr(ipaddress.ip_address(a[4][0]), "ipv4_mapped", None)
            for a in addresses
        ):
            raise DomainError("UPLOAD_SOURCE_DENIED", "File host must resolve to public addresses")
        family, socktype, proto, _, address = addresses[0]
        raw_socket = socket.socket(family, socktype, proto)
        raw_socket.settimeout(remaining())
        raw_socket.connect(address)
        # Connect to the already checked address, but authenticate the original hostname.
        # No proxy environment, redirects, cookies or vault/OAuth credentials are used.
        tls = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=url.hostname)
        raw_socket = tls

        def abort():
            # A peer trickling HTTP headers/chunk framing must not extend the total deadline.
            try:
                tls.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        watchdog = threading.Timer(max(0, deadline - time.monotonic()), abort)
        watchdog.daemon = True
        watchdog.start()
        conn = http.client.HTTPSConnection(url.hostname, timeout=remaining())
        conn.sock = tls
        target = (url.path or "/") + ("?" + url.query if url.query else "")
        conn.request("GET", target, headers={"Accept-Encoding": "identity"})
        tls.settimeout(remaining())
        response = conn.getresponse()
        if response.status != 200:
            raise DomainError(
                "UPLOAD_DOWNLOAD_FAILED",
                "File download was rejected or redirected. Get a fresh file reference and retry.",
                response.status >= 500,
            )
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise DomainError("UPLOAD_DOWNLOAD_FAILED", "Encoded file responses are not supported")
        length = response.getheader("Content-Length")
        transfer = response.getheader("Transfer-Encoding")
        if transfer and (transfer.lower() != "chunked" or length is not None):
            raise DomainError("UPLOAD_DOWNLOAD_FAILED", "Ambiguous file response framing")
        if length is not None:
            if not re.fullmatch(r"[0-9]+", length):
                raise DomainError("UPLOAD_DOWNLOAD_FAILED", "Invalid file length")
            if int(length) > settings.upload_max_file_bytes:
                raise upload_limit(settings)
        result = bytearray()
        while not response.isclosed():
            tls.settimeout(remaining())
            chunk = response.read1(min(65536, settings.upload_max_file_bytes + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > settings.upload_max_file_bytes:
                raise upload_limit(settings)
        if length is not None and len(result) != int(length):
            raise DomainError("UPLOAD_DOWNLOAD_FAILED", "Incomplete file download; retry")
        return bytes(result)
    except (OSError, http.client.HTTPException, ValueError):
        # Network exceptions can contain signed URLs. Only expose a fixed message.
        raise DomainError(
            "UPLOAD_DOWNLOAD_FAILED", "File download failed or timed out; retry", True
        ) from None
    finally:
        if watchdog:
            watchdog.cancel()
        if response:
            response.close()
        if conn:
            conn.close()
        if raw_socket:
            raw_socket.close()


def upload_limit(settings):
    return DomainError(
        "UPLOAD_TOO_LARGE", f"File exceeds UPLOAD_MAX_FILE_BYTES={settings.upload_max_file_bytes}"
    )


class UploadStore:
    """Staging lives in the bound state DB. Expiry never affects prepared Git trees."""

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, repo TEXT NOT NULL,
                idem TEXT NOT NULL, file_id TEXT NOT NULL, sha256 TEXT NOT NULL,
                size INTEGER NOT NULL, expires REAL NOT NULL, content BLOB NOT NULL,
                UNIQUE(owner,repo,idem))""")

    def stage(self, file, key, actor, authorize):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise DomainError("INVALID_OPERATION", "Use a 1-128 character ASCII idempotency key")
        # Serial downloads bound simultaneous memory and make quota enforcement deterministic.
        with file_lock(self.settings.data_root / ".upload.lock"):
            authorize(actor)
            self.cleanup()
            with self.store.connect() as db:
                count, size = db.execute(
                    "SELECT count(*),coalesce(sum(size),0) FROM uploads"
                ).fetchone()
                prior = db.execute(
                    "SELECT id FROM uploads WHERE owner=? AND repo=? AND idem=?",
                    (actor.subject, self.store.repo_id, key),
                ).fetchone()
                if not prior and (count >= 1000 or size >= self.settings.upload_staging_max_bytes):
                    raise DomainError(
                        "UPLOAD_QUOTA", "Upload staging quota reached; wait for expiry"
                    )
            raw = download_file(file, self.settings)
            authorize(actor)
            return self.save(file.file_id, raw, key, actor.subject)

    def save(self, file_id, raw, key, owner):
        if len(raw) > self.settings.upload_max_file_bytes:
            raise upload_limit(self.settings)
        digest = hashlib.sha256(raw).hexdigest()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM uploads WHERE expires<=?", (time.time(),))
            prior = db.execute(
                "SELECT id,file_id,sha256,size,expires FROM uploads "
                "WHERE owner=? AND repo=? AND idem=?",
                (owner, self.store.repo_id, key),
            ).fetchone()
            if prior:
                if prior[1] != file_id or prior[2] != digest:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "Upload key belongs to another file")
                id, _, digest, size, expires = prior
            else:
                count, used = db.execute(
                    "SELECT count(*),coalesce(sum(size),0) FROM uploads"
                ).fetchone()
                if count >= 1000 or used + len(raw) > self.settings.upload_staging_max_bytes:
                    raise DomainError(
                        "UPLOAD_QUOTA", "Upload staging quota reached; wait for expiry"
                    )
                id, size = "upl_" + uuid.uuid4().hex, len(raw)
                expires = time.time() + self.settings.upload_retention_seconds
                db.execute(
                    "INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        id,
                        owner,
                        self.store.repo_id,
                        key,
                        file_id,
                        digest,
                        size,
                        expires,
                        raw,
                    ),
                )
        return {
            "upload_id": id,
            "sha256": digest,
            "size_bytes": size,
            "expires_at": expires,
            "status": "staged",
            "attachments_folder": self.settings.attachments_folder,
            "max_file_bytes": self.settings.upload_max_file_bytes,
        }

    def read(self, id, owner):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT content,sha256,expires FROM uploads WHERE id=? AND owner=? AND repo=?",
                (id, owner, self.store.repo_id),
            ).fetchone()
        if not row or row[2] <= time.time():
            raise DomainError("UPLOAD_NOT_FOUND", "Upload is unavailable or expired; upload again")
        raw, digest, _ = row
        if len(raw) > self.settings.upload_max_file_bytes:
            raise upload_limit(self.settings)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise DomainError("UPLOAD_CORRUPT", "Staged file no longer matches its digest")
        return raw

    def cleanup(self):
        with self.store.connect() as db:
            db.execute("DELETE FROM uploads WHERE expires<=?", (time.time(),))
