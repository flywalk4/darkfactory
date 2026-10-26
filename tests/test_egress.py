import socket
import socketserver
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from darkfactory.egress import egress_proxy, local_proxy, local_tunnel


@pytest.fixture
def socket_path():
    # macOS limits Unix socket paths to 104 bytes; pytest's tmp_path is longer.
    with tempfile.TemporaryDirectory(prefix="df-egress-", dir="/tmp") as directory:
        yield Path(directory) / "egress.sock"


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        while data := self.request.recv(4096):
            self.request.sendall(data)
        self.request.sendall(b"finished")


@contextmanager
def echo_server():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def connect_proxy(url):
    parsed = urlsplit(url)
    return socket.create_connection((parsed.hostname, parsed.port), timeout=2)


def response(connection):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def test_allowed_tunnel_and_half_close(socket_path):
    path = socket_path
    with echo_server() as (host, port):
        with egress_proxy(path, {(host, port)}), local_proxy(str(path)) as url:
            assert path.stat().st_mode & 0o777 == 0o666
            with connect_proxy(url) as client:
                client.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())
                assert response(client) == b"HTTP/1.1 200 Connection Established\r\n\r\n"
                client.sendall(b"hello")
                assert client.recv(5) == b"hello"
                client.shutdown(socket.SHUT_WR)
                assert client.recv(8) == b"finished"
                assert client.recv(1) == b""
    assert not path.exists()


def test_plain_http_forwarded_to_allowed_host_only(socket_path):
    with echo_server() as (host, port):
        with egress_proxy(socket_path, {(host, port)}), local_proxy(str(socket_path)) as url:
            with connect_proxy(url) as client:
                client.sendall(
                    f"GET http://{host}:{port}/v3/index.json?x=1 HTTP/1.1\r\nHost: {host}\r\n"
                    "Proxy-Connection: keep-alive\r\n\r\n".encode()
                )
                forwarded = response(client)
    assert forwarded.startswith(b"GET /v3/index.json?x=1 HTTP/1.1\r\n")
    assert b"Proxy-Connection" not in forwarded and b"Connection: close" in forwarded


@pytest.mark.parametrize(
    "payload, status",
    [
        (b"CONNECT example.org:443 HTTP/1.1\r\n\r\n", b"403 Forbidden"),
        (b"CONNECT api.example.com.evil.org:443 HTTP/1.1\r\n\r\n", b"403 Forbidden"),
        (b"CONNECT api.example.com:444 HTTP/1.1\r\n\r\n", b"403 Forbidden"),
        (b"CONNECT allowed@api.example.com:443 HTTP/1.1\r\n\r\n", b"400 Bad Request"),
        (b"CONNECT api.example.com:443/path HTTP/1.1\r\n\r\n", b"400 Bad Request"),
        (b"GET http://api.example.com/ HTTP/1.1\r\n\r\n", b"403 Forbidden"),
        (b"GET / HTTP/1.1\r\n\r\n", b"405 Method Not Allowed"),
        (b"GET https://api.example.com/ HTTP/1.1\r\n\r\n", b"405 Method Not Allowed"),
    ],
)
def test_reject_unapproved_destinations(socket_path, payload, status):
    path = socket_path
    with egress_proxy(path, {("api.example.com", 443)}), local_proxy(str(path)) as url:
        with connect_proxy(url) as client:
            client.sendall(payload)
            assert status in response(client)


def test_shutdown_closes_live_tunnel_and_loopback_port(socket_path):
    path = socket_path
    with echo_server() as (host, port):
        started = time.monotonic()
        with egress_proxy(path, {(host, port)}):
            with local_proxy(str(path)) as url:
                client = connect_proxy(url)
                client.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())
                assert b"200 Connection Established" in response(client)
            assert client.recv(1) == b""
            client.close()
            with pytest.raises(OSError):
                connect_proxy(url)
        assert time.monotonic() - started < 2


def test_pipeline_bytes_not_lost(socket_path):
    path = socket_path
    with echo_server() as (host, port):
        with egress_proxy(path, {(host, port)}), local_proxy(str(path)) as url:
            with connect_proxy(url) as client:
                client.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\nearly".encode())
                received = response(client)
                while not received.endswith(b"early"):
                    received += client.recv(5)
                assert received.endswith(b"\r\n\r\nearly")


def test_fixed_gateway_uses_allowlisted_tunnel_and_closes(socket_path):
    with echo_server() as (host, port), egress_proxy(socket_path, {(host, port)}):
        with local_tunnel(str(socket_path), host, port) as url:
            with connect_proxy(url) as client:
                client.sendall(b"gateway-request")
                assert client.recv(15) == b"gateway-request"
                client.shutdown(socket.SHUT_WR)
                assert client.recv(8) == b"finished"
                assert client.recv(1) == b""
            live = connect_proxy(url)
            live.sendall(b"ping")
            assert live.recv(4) == b"ping"
        assert live.recv(1) == b""
        live.close()
        with pytest.raises(OSError):
            connect_proxy(url)


def test_fixed_gateway_cannot_bypass_egress_allowlist(socket_path):
    with echo_server() as (host, port), egress_proxy(socket_path, set()):
        with local_tunnel(str(socket_path), host, port) as url:
            with connect_proxy(url) as client:
                assert client.recv(1) == b""


class Bulk(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(b"x" * (4 * 1024 * 1024))


class BulkServer(socketserver.ThreadingTCPServer):
    request_queue_size = 64


def test_burst_of_parallel_tunnels_and_slow_reader_survive(socket_path):
    server = BulkServer(("127.0.0.1", 0), Bulk)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    host, port = server.server_address
    results = []

    def fetch(url, slow):
        with connect_proxy(url) as connection:
            connection.settimeout(30)
            connection.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())
            head = response(connection)
            body = len(head.split(b"\r\n\r\n", 1)[1])
            if slow:
                time.sleep(7)  # Longer than the per-operation socket timeout.
            while chunk := connection.recv(65536):
                body += len(chunk)
            results.append((head.split(b"\r\n", 1)[0], body))

    try:
        with egress_proxy(socket_path, {(host, port)}), local_proxy(str(socket_path)) as url:
            threads = [threading.Thread(target=fetch, args=(url, index == 0)) for index in range(40)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
    finally:
        server.shutdown()
        server.server_close()
    assert len(results) == 40
    assert [
        item for item in results if item != (b"HTTP/1.1 200 Connection Established", 4 * 1024 * 1024)
    ] == []


def test_tunnel_outlasts_a_model_thinking_before_its_first_byte():
    # Codex saw "error sending request" every 120 s and retried a large prompt from scratch.
    from darkfactory import egress

    assert egress._IDLE_TIMEOUT >= 600


@pytest.mark.parametrize(
    "address, refused",
    [
        ("127.0.0.1", True),
        ("10.1.2.3", True),
        ("172.18.0.2", True),
        ("192.168.1.1", True),
        ("169.254.169.254", True),
        ("100.64.0.1", True),
        ("240.0.0.1", True),
        ("224.0.0.1", True),
        ("::1", True),
        ("fe80::1", True),
        ("::ffff:127.0.0.1", True),
        ("93.184.216.34", False),
        ("2606:4700::1111", False),
    ],
)
def test_private_and_special_addresses_are_refused(address, refused):
    from darkfactory.egress import refused_address

    assert refused_address(address) is refused


def test_private_destination_refused_unless_listed_as_ip_literal(socket_path):
    with echo_server() as (host, port):
        with egress_proxy(socket_path, {(host, port), ("localhost", port)}, private=set()):
            with local_proxy(str(socket_path)) as url:
                for authority in (f"{host}:{port}", f"localhost:{port}"):
                    with connect_proxy(url) as client:
                        client.sendall(f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode())
                        assert b"403 Forbidden" in response(client)
        with egress_proxy(socket_path, {(host, port)}, private={(host, port)}):
            with local_proxy(str(socket_path)) as url, connect_proxy(url) as client:
                client.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())
                assert b"200 Connection Established" in response(client)


def test_name_is_resolved_once_and_dialled_by_checked_address(socket_path, monkeypatch):
    from darkfactory import egress

    calls = []

    def rebinding(host, port):
        # The first answer is checked; a second lookup would point somewhere else.
        calls.append(host)
        return [(socket.AF_INET, ("127.0.0.1" if len(calls) == 1 else "10.9.9.9", port))]

    monkeypatch.setattr(egress, "resolve", rebinding)
    with echo_server() as (host, port):
        allowed = {("feed.example.com", port)}
        with egress_proxy(socket_path, allowed, private=set()), local_proxy(str(socket_path)) as url:
            with connect_proxy(url) as client:
                client.sendall(f"CONNECT feed.example.com:{port} HTTP/1.1\r\n\r\n".encode())
                assert b"403 Forbidden" in response(client)
        calls.clear()
        with egress_proxy(socket_path, allowed, private={(host, port)}), local_proxy(str(socket_path)) as url:
            with connect_proxy(url) as client:
                client.sendall(f"CONNECT feed.example.com:{port} HTTP/1.1\r\n\r\n".encode())
                assert b"200 Connection Established" in response(client)
                client.sendall(b"ping")
                assert client.recv(4) == b"ping"
    assert calls == ["feed.example.com"]


def test_plain_http_host_header_is_the_destination(socket_path):
    with echo_server() as (host, port):
        with egress_proxy(socket_path, {(host, port)}), local_proxy(str(socket_path)) as url:
            with connect_proxy(url) as client:
                client.sendall(f"GET http://{host}:{port}/ HTTP/1.1\r\nHost: db:5432\r\n\r\n".encode())
                forwarded = response(client)
    assert f"\r\nHost: {host}:{port}\r\n".encode() in forwarded
    assert b"db:5432" not in forwarded


def test_concurrent_tunnels_are_capped(socket_path, monkeypatch):
    from darkfactory import egress

    monkeypatch.setattr(egress, "_MAX_TUNNELS", 1)
    monkeypatch.setattr(egress, "_CONNECT_TIMEOUT", 0.5)
    with echo_server() as (host, port):
        with egress_proxy(socket_path, {(host, port)}), local_proxy(str(socket_path)) as url:
            with connect_proxy(url) as first:
                first.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())
                assert b"200 Connection Established" in response(first)
                with connect_proxy(url) as second:
                    second.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\n\r\n".encode())
                    assert b"503 Service Unavailable" in response(second)
