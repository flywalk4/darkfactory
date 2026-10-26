"""Loopback credential relay inside a task container.

The agent CLI talks to http(s)://127.0.0.1:<port> with a placeholder credential; the relay, running in the
root-owned runner, replaces the credential headers with the real ones and forwards the request over HTTPS
(through the task's egress proxy). The real token never enters the agent's environment or memory, so a
prompt-injected agent cannot read it from /proc and send it anywhere. Responses, including server-sent
event streams, are passed back as they arrive.
"""

import datetime
import ipaddress
import os
import ssl
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

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


def loopback_certificate():
    """A one-off CA and a server certificate it signs for 127.0.0.1, as PEM: (ca, cert, key).

    The two must differ: rustls in Codex refuses a CA certificate as a server's, and with a self-signed
    CA=true certificate the connection silently drops."""
    start = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    end = start + datetime.timedelta(days=2)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Darkfactory relay CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    return (
        ca.public_bytes(pem),
        cert.public_bytes(pem),
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )


def _tls_context(cert_pem, key_pem):
    """ssl loads a key only from a file: it lives in a private temporary file for the load alone."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    handle, path = tempfile.mkstemp(suffix=".pem")
    try:
        os.chmod(path, 0o600)
        with os.fdopen(handle, "wb") as file:
            file.write(cert_pem + key_pem)
        context.load_cert_chain(path)
    finally:
        os.unlink(path)
    return context


@contextmanager
def auth_relay(upstream, credentials, tls=None):
    """Serve a loopback base URL that forwards to `upstream` with `credentials` headers added.

    With `tls=(cert_pem, key_pem)` the relay serves HTTPS: Codex reaches the ChatGPT backend only over it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    if tls:
        server.socket = _tls_context(*tls).wrap_socket(
            # The handshake runs in the request's thread, so a stalled client cannot block accept().
            server.socket,
            server_side=True,
            do_handshake_on_connect=False,
        )
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
        yield f"{'https' if tls else 'http'}://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        server.client.close()
        thread.join(timeout=5)
