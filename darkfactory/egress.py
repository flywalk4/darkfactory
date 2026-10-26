"""Task-scoped CONNECT egress over Unix sockets, with a runner-local TCP bridge."""

import ipaddress
import os
import selectors
import socket
import socketserver
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

# Public dependency registries a project may allow as a group (`package_registries`).
PACKAGE_REGISTRIES = frozenset(
    (host, 443)
    for host in (
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        "pypi.org",
        "files.pythonhosted.org",
        "api.nuget.org",
        "repo.maven.apache.org",
        "repo1.maven.org",
        "services.gradle.org",
        "plugins.gradle.org",
        "proxy.golang.org",
        "sum.golang.org",
        "index.crates.io",
        "static.crates.io",
        "rubygems.org",
        "nodejs.org",
    )
)

# Agent CLIs and headless browsers call home to these; refusing them is expected and never blocks the task.
TELEMETRY_SUFFIXES = (
    "update.googleapis.com",
    "content-autofill.googleapis.com",
    "safebrowsing.googleapis.com",
    "optimizationguide-pa.googleapis.com",
    "clientservices.googleapis.com",
    "accounts.google.com",
    "clients2.google.com",
    "www.google.com",
    "gvt1.com",
    "openai.com",
    "chatgpt.com",
    "anthropic.com",
    "claude.ai",
    "sentry.io",
    "statsig.com",
    "statsigapi.net",
    "datadoghq.com",
    "segment.io",
)

_CONNECT_TIMEOUT = 10
_IO_TIMEOUT = 5
# A model may think for minutes before its first byte; a shorter cut makes the client retry from scratch.
_IDLE_TIMEOUT = max(60, int(os.getenv("DARKFACTORY_EGRESS_IDLE_SECONDS", "900")))
_MAX_HEADER = 32 * 1024
_MAX_TUNNELS = 128
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Operator-set Compose mirrors and the personal-mode host gateway live on private addresses by design.
_OPERATOR_MIRRORS = ("DARKFACTORY_NPM_MIRROR", "DARKFACTORY_PYPI_MIRROR")
_PERSONAL_GATEWAYS = frozenset({"localhost", "host.docker.internal"})


def refused_address(address: str) -> bool:
    """True for loopback, private, link-local, CGNAT, reserved and multicast addresses."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast or ip in _CGNAT


def ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def single_label(host: str) -> bool:
    return "." not in host.rstrip(".") and not ip_literal(host)


def resolve(host: str, port: int) -> list[tuple]:
    """Resolve once; callers connect to these exact socket addresses."""
    return [
        (family, address)
        for family, _, _, _, address in socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_STREAM)
    ]


def _host(value: str) -> str:
    value = value.rstrip(".").lower()
    if not value or any(char in value for char in "%/@?#\\ \t\r\n"):
        raise ValueError("Invalid proxy destination")
    return value.encode("idna").decode("ascii")


def _destination(authority: str) -> tuple[str, int]:
    parsed = urlsplit("//" + authority)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
    ):
        raise ValueError("CONNECT requires an exact host and port")
    return _host(parsed.hostname), parsed.port


class _Server(socketserver.ThreadingMixIn):
    # server_close joins every handler after shutdown closes its sockets.
    daemon_threads = False
    block_on_close = True
    # Package managers open dozens of tunnels at once; the default backlog of 5 refused them.
    request_queue_size = 256

    def __init__(self, address, handler):
        self.stopping = threading.Event()
        self.tunnels = threading.BoundedSemaphore(_MAX_TUNNELS)
        self.connections = set()
        self.connections_lock = threading.Lock()
        super().__init__(address, handler)

    @contextmanager
    def connection(self, connection):
        with self.connections_lock:
            if self.stopping.is_set():
                connection.close()
                raise OSError("Proxy is stopping")
            self.connections.add(connection)
        try:
            connection.settimeout(_IO_TIMEOUT)
            yield connection
        finally:
            with self.connections_lock:
                self.connections.discard(connection)
            connection.close()

    def stop(self):
        self.stopping.set()
        with self.connections_lock:
            for connection in self.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.shutdown()
        self.server_close()


class _UnixServer(_Server, socketserver.UnixStreamServer):
    pass


class _TcpServer(_Server, socketserver.TCPServer):
    allow_reuse_address = True


def _relay(left, right, stopping):
    # Setup used short timeouts; an established tunnel waits on a slow reader as long as on silence.
    left.settimeout(_IDLE_TIMEOUT)
    right.settimeout(_IDLE_TIMEOUT)
    peers = {left: right, right: left}
    last_activity = time.monotonic()
    # select.select fails on descriptors above FD_SETSIZE, which a busy worker reaches.
    with selectors.DefaultSelector() as selector:
        for source in peers:
            selector.register(source, selectors.EVENT_READ)
        while selector.get_map() and not stopping.is_set():
            if time.monotonic() - last_activity >= _IDLE_TIMEOUT:
                return
            for key, _ in selector.select(0.2):
                source = key.fileobj
                data = source.recv(65536)
                if not data:
                    selector.unregister(source)
                    try:
                        peers[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                last_activity = time.monotonic()
                peers[source].sendall(data)


class _Connect(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with self.server.connection(self.request):
                if not self.server.tunnels.acquire(timeout=_CONNECT_TIMEOUT):
                    self.request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
                    return
                try:
                    self.connect()
                finally:
                    self.server.tunnels.release()
        except (OSError, ValueError):
            # Disconnects and bounded socket timeouts terminate this tunnel only.
            return

    def addresses(self, destination):
        """Resolved addresses this destination may use; private ones only when the operator listed them."""
        host, port = destination
        private = destination in self.server.private
        if single_label(host) and not private:
            return []
        try:
            resolved = resolve(host, port)
        except (OSError, UnicodeError):
            return None
        return [
            (family, address)
            for family, address in resolved
            if private or not refused_address(address[0]) or (address[0], port) in self.server.private
        ]

    def connect(self):
        header = bytearray()
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        while b"\r\n\r\n" not in header:
            if len(header) >= _MAX_HEADER or time.monotonic() >= deadline:
                self.request.sendall(b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\n")
                return
            data = self.request.recv(min(4096, _MAX_HEADER - len(header)))
            if not data:
                return
            header.extend(data)
        request, pending = bytes(header).split(b"\r\n\r\n", 1)
        forward = None
        try:
            method, authority, version = request.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            if method != "CONNECT":
                # Plain-HTTP package feeds (e.g. a corporate NuGet on :80) arrive in absolute form.
                if not authority.startswith("http://"):
                    self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
                    return
                url = urlsplit(authority)
                forward = (method, (url.path or "/") + ("?" + url.query if url.query else ""))
                authority = url.netloc if url.port else url.netloc + ":80"
            destination = _destination(authority)
            if version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ValueError("Invalid proxy request")
        except (ValueError, UnicodeError):
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        if destination not in self.server.allowed:
            if self.server.denied is not None and len(self.server.denied) < 50:
                self.server.denied.add(f"{destination[0]}:{destination[1]}")
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        addresses = self.addresses(destination)
        if addresses == []:
            if self.server.denied is not None and len(self.server.denied) < 50:
                self.server.denied.add(f"{destination[0]}:{destination[1]}")
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        # Connect to the address just checked: a second lookup could rebind the name to a private one.
        target = None
        for family, address in addresses or []:
            candidate = socket.socket(family, socket.SOCK_STREAM)
            candidate.settimeout(_CONNECT_TIMEOUT)
            try:
                candidate.connect(address)
            except OSError:
                candidate.close()
                continue
            target = candidate
            break
        if target is None:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        with self.server.connection(target):
            if forward:
                # One request per connection keeps the allowlist decision bound to this destination.
                headers = [
                    line
                    for line in request.split(b"\r\n")[1:]
                    if line.split(b":", 1)[0].strip().lower()
                    not in {
                        b"connection",
                        b"proxy-connection",
                        b"proxy-authorization",
                        b"keep-alive",
                        b"host",
                    }
                ]
                host = f"[{destination[0]}]" if ":" in destination[0] else destination[0]
                host = host if destination[1] == 80 else f"{host}:{destination[1]}"
                headers.insert(0, b"Host: " + host.encode("ascii"))
                first = f"{forward[0]} {forward[1]} {version}".encode("ascii")
                target.sendall(b"\r\n".join([first, *headers, b"Connection: close"]) + b"\r\n\r\n")
            else:
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if pending:
                target.sendall(pending)
            _relay(self.request, target, self.server.stopping)


class _Bridge(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with self.server.connection(self.request):
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                with self.server.connection(upstream):
                    upstream.connect(self.server.unix_path)
                    _relay(self.request, upstream, self.server.stopping)
        except (OSError, ValueError):
            return


class _Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with self.server.connection(self.request):
                upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                with self.server.connection(upstream):
                    upstream.connect(self.server.unix_path)
                    upstream.sendall(f"CONNECT {self.server.authority} HTTP/1.1\r\n\r\n".encode("ascii"))
                    header = bytearray()
                    deadline = time.monotonic() + _CONNECT_TIMEOUT
                    while b"\r\n\r\n" not in header:
                        if len(header) >= _MAX_HEADER or time.monotonic() >= deadline:
                            raise OSError("Tunnel negotiation timed out")
                        chunk = upstream.recv(min(4096, _MAX_HEADER - len(header)))
                        if not chunk:
                            raise OSError("Proxy closed tunnel negotiation")
                        header.extend(chunk)
                    status, pending = bytes(header).split(b"\r\n\r\n", 1)
                    if status.split(b"\r\n", 1)[0] != b"HTTP/1.1 200 Connection Established":
                        raise OSError("Proxy rejected tunnel")
                    if pending:
                        self.request.sendall(pending)
                    _relay(self.request, upstream, self.server.stopping)
        except (OSError, ValueError):
            return


@contextmanager
def _running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stop()
        thread.join()


def _operator_private(allowed):
    private = set()
    for name in _OPERATOR_MIRRORS:
        if url := os.getenv(name):
            parts = urlsplit(url)
            if parts.hostname:
                private.add((_host(parts.hostname), parts.port or (443 if parts.scheme == "https" else 80)))
    if os.getenv("DARKFACTORY_PERSONAL_MODE") == "1":
        private |= {(host, port) for host, port in allowed if host in _PERSONAL_GATEWAYS}
    return private & allowed


@contextmanager
def egress_proxy(
    socket_path: Path,
    allowed: set[tuple[str, int]],
    denied: set[str] | None = None,
    private: set[tuple[str, int]] | None = None,
):
    """Allow CONNECT only to explicit destinations; each name is resolved once and dialled by address.

    A destination resolving to a loopback, private, link-local, CGNAT, reserved or multicast address, or a
    single-label name, is refused unless it is in `private`: the IP-literal `host:port` entries the operator
    listed. When `private` is omitted, IP literals in `allowed` count as operator-listed.

    Refused destinations are collected into `denied` (bounded) so operators see what the task needed.

    This is a byte tunnel: TLS, credentials and redirects remain the client's concern.
    No arbitrary worker-side URL fetching or HTTP forwarding is supported.
    """
    normalized = {(_host(host), int(port)) for host, port in allowed}
    if any(not 1 <= port <= 65535 for _, port in normalized):
        raise ValueError("Invalid allowed destination port")
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = _UnixServer(str(socket_path), _Connect)
    server.allowed = normalized
    server.denied = denied
    if private is None:
        private = {(host, port) for host, port in normalized if ip_literal(host)}
    server.private = {(_host(host), int(port)) for host, port in private} | _operator_private(normalized)
    try:
        socket_path.chmod(0o666)
        with _running(server):
            yield
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


@contextmanager
def local_proxy(unix_path: str):
    """Expose worker Unix proxy as an ephemeral loopback HTTP proxy inside a task."""
    server = _TcpServer(("127.0.0.1", 0), _Bridge)
    server.unix_path = unix_path
    with _running(server):
        yield f"http://127.0.0.1:{server.server_address[1]}"


@contextmanager
def local_tunnel(unix_path: str, host: str, port: int):
    """Bridge one fixed HTTP gateway through the existing CONNECT allowlist."""
    authority = f"{_host(host)}:{port}"
    _destination(authority)
    server = _TcpServer(("127.0.0.1", 0), _Tunnel)
    server.unix_path, server.authority = unix_path, authority
    with _running(server):
        yield f"http://127.0.0.1:{server.server_address[1]}"
