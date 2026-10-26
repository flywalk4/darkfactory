"""Loopback credential relay inside a task container.

The agent CLI talks to http://127.0.0.1:<port> with a placeholder credential; the relay, running in the
root-owned runner, replaces the credential headers with the real ones and forwards the request over HTTPS
(through the task's egress proxy). The real token never enters the agent's environment or memory, so a
prompt-injected agent cannot read it from /proc and send it anywhere. Responses, including server-sent
event streams, are passed back as they arrive.
"""

import os
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

# Headers the relay sets or the client must not choose.
CREDENTIAL_HEADERS = {"authorization", "x-api-key", "cookie"}
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
PLACEHOLDER = "darkfactory-relay"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_HEADERS | CREDENTIAL_HEADERS
        }
        headers.update(self.server.credentials)
        # Plain bodies only: the relay passes bytes through, and a client must not get an encoding it
        # negotiated with the relay but cannot decode (a live Codex rate-limit read failed that way).
        headers = {key: value for key, value in headers.items() if key.lower() != "accept-encoding"}
        headers["Accept-Encoding"] = "identity"
        try:
            with self.server.client.stream(
                self.command, self.server.upstream + self.path, headers=headers, content=body
            ) as response:
                self.send_response(response.status_code)
                for key, value in response.headers.multi_items():
                    if key.lower() not in HOP_HEADERS:
                        self.send_header(key, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in response.iter_raw():
                    if chunk:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except httpx.HTTPError as exc:
            message = f"darkfactory relay: upstream unreachable ({type(exc).__name__})".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward


@contextmanager
def auth_relay(upstream, credentials):
    """Serve a loopback base URL that forwards to `upstream` with `credentials` headers added."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.upstream = upstream.rstrip("/")
    server.credentials = credentials
    # HTTPS_PROXY is the task's egress proxy; the relay's own requests obey the same allowlist. A loopback
    # upstream (the personal gateway's local tunnel) is reached directly.
    loopback = urlsplit(server.upstream).hostname in {"127.0.0.1", "localhost", "::1"}
    proxy = None if loopback else os.environ.get("HTTPS_PROXY")
    server.client = httpx.Client(timeout=httpx.Timeout(None, connect=30), proxy=proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        server.client.close()
        thread.join(timeout=5)
