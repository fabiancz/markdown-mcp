"""Disposable HTTPS smart-Git fixture with synthetic Basic credentials."""

import base64
import os
import ssl
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


@contextmanager
def https_git(root: Path):
    cert, key = root / "cert.pem", root / "key.pem"
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
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    backend = (
        subprocess.check_output(["git", "--exec-path"], text=True).strip() + "/git-http-backend"
    )
    records = []
    expected = "Basic " + base64.b64encode(b"fixture:test-only").decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_git()

        def do_POST(self):
            self.handle_git()

        def handle_git(self):
            records.append(
                {"path": self.path, "authorized": self.headers.get("Authorization") == expected}
            )
            if self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"https://localhost:{self.server.server_port}/other/vault.git/info/refs",
                )
                self.end_headers()
                return
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                self.end_headers()
                return
            url = urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            env = dict(
                os.environ,
                GIT_PROJECT_ROOT=str(root),
                GIT_HTTP_EXPORT_ALL="1",
                PATH_INFO=url.path,
                QUERY_STRING=url.query,
                REQUEST_METHOD=self.command,
                CONTENT_TYPE=self.headers.get("Content-Type", ""),
                CONTENT_LENGTH=str(len(body)),
                REMOTE_USER="fixture",
            )
            result = subprocess.run(
                [backend], input=body, env=env, capture_output=True, check=True
            ).stdout
            headers, content = result.split(b"\r\n\r\n", 1)
            fields = [line.decode().split(":", 1) for line in headers.split(b"\r\n")]
            status = next(
                (int(v.strip().split()[0]) for k, v in fields if k.lower() == "status"), 200
            )
            self.send_response(status)
            for k, v in fields:
                if k.lower() != "status":
                    self.send_header(k, v.strip())
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, cert, records
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
