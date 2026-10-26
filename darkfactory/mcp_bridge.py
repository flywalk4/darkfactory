"""Task-local MCP gateways enforce project tool permissions before forwarding."""

import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx

from .privileges import MCP_UID, as_user, hand_over

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 30
HTTP_CLIENT = httpx.Client
METHODS = {"initialize", "ping", "tools/list", "tools/call"}
NOTIFICATIONS = {"notifications/initialized", "notifications/cancelled"}
SAFE_ENV = {
    "PATH",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}


def rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _response(value, request_id):
    if not isinstance(value, dict) or value.get("id") != request_id or "method" in value:
        raise ValueError("Invalid MCP response")
    if "result" not in value and "error" not in value:
        raise ValueError("Missing MCP response")
    return value


class HTTPUpstream:
    def __init__(self, config):
        endpoint = urlsplit(config.get("url") or "")
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password:
            raise ValueError("MCP upstream requires HTTPS")
        self.url = config["url"]
        self.headers = dict(config.get("headers") or {})
        self.client = HTTP_CLIENT(timeout=TIMEOUT, follow_redirects=False, trust_env=True)
        self.session = None
        self.protocol = None

    def _remember(self, value, message):
        value = _response(value, message["id"])
        if message["method"] == "initialize" and isinstance(value.get("result"), dict):
            version = value["result"].get("protocolVersion")
            if isinstance(version, str) and len(version) <= 100 and not any(c in version for c in "\r\n"):
                self.protocol = version
        return value

    def request(self, message):
        headers = {**self.headers, "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["MCP-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        with self.client.stream("POST", self.url, headers=headers, json=message) as response:
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise ValueError("MCP redirects are disabled")
            if response.headers.get("MCP-Session-Id"):
                self.session = response.headers["MCP-Session-Id"]
            if "id" not in message:
                return None
            body, started = bytearray(), time.monotonic()
            sse = "text/event-stream" in response.headers.get("Content-Type", "")
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES or time.monotonic() - started > TIMEOUT:
                    raise ValueError("MCP response limit exceeded")
                if sse:
                    normalized = bytes(body).replace(b"\r\n", b"\n")
                    while b"\n\n" in normalized:
                        frame, normalized = normalized.split(b"\n\n", 1)
                        data = b"\n".join(
                            line[5:].lstrip() for line in frame.splitlines() if line.startswith(b"data:")
                        )
                        if data:
                            value = json.loads(data)
                            if isinstance(value, dict) and value.get("id") == message["id"]:
                                return self._remember(value, message)
                    # Keep the total byte count bounded even when frames are consumed.
                    # Parsing all frames again is harmless and preserves the size bound.
            if sse:
                raise ValueError("MCP SSE response missing")
            return self._remember(json.loads(body), message)

    def close(self):
        self.client.close()


class StdioUpstream:
    def __init__(self, config):
        env = {key: value for key, value in os.environ.items() if key in SAFE_ENV}
        # The runner's HOME belongs to another uid; servers that keep caches there (fastmcp) exit at start.
        self.home = tempfile.mkdtemp(prefix="mcp-home-")
        hand_over(self.home, uid=MCP_UID)
        env["HOME"] = self.home
        env.update(config.get("env") or {})
        # `${HTTPS_PROXY}` in an argument: a browser takes the task's egress proxy only as a flag.
        args = [
            re.sub(
                r"\$\{(\w+)\}",
                lambda m: env.get(m.group(1), "") if m.group(1) in SAFE_ENV else m.group(0),
                arg,
            )
            for arg in config.get("args", [])
        ]
        self.process = subprocess.Popen(
            [config["command"], *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            # Not the workspace: it is the agent's and unreadable here, and servers read a .env from their
            # working directory (mcp-atlassian with override=True), so a repository file could redirect them.
            cwd=self.home,
            start_new_session=True,
            # Its own uid: the agent cannot read this server's environment secrets from /proc.
            **as_user(MCP_UID),
        )
        os.set_blocking(self.process.stdin.fileno(), False)
        self.lock, self.write_lock = threading.Lock(), threading.Lock()
        self.buffer = bytearray()

    def _write(self, message):
        payload = memoryview(json.dumps(message).encode() + b"\n")
        if len(payload) > MAX_BYTES:
            raise ValueError("MCP request limit exceeded")
        if not self.write_lock.acquire(timeout=TIMEOUT):
            raise TimeoutError("MCP process write timed out")
        try:
            deadline = time.monotonic() + TIMEOUT
            while payload:
                _, ready, _ = select.select([], [self.process.stdin], [], max(0, deadline - time.monotonic()))
                if not ready:
                    raise TimeoutError("MCP process write timed out")
                try:
                    payload = payload[os.write(self.process.stdin.fileno(), payload) :]
                except BlockingIOError:
                    continue
        finally:
            self.write_lock.release()

    def request(self, message):
        if "id" not in message:
            self._write(message)
            return None
        with self.lock:
            self._write(message)
            deadline, received = time.monotonic() + TIMEOUT, 0
            while time.monotonic() < deadline:
                if b"\n" not in self.buffer:
                    ready, _, _ = select.select(
                        [self.process.stdout], [], [], max(0, deadline - time.monotonic())
                    )
                    if not ready:
                        break
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise ValueError("MCP process closed output")
                    received += len(chunk)
                    self.buffer.extend(chunk)
                    if received > MAX_BYTES or len(self.buffer) > MAX_BYTES:
                        raise ValueError("MCP response limit exceeded")
                while b"\n" in self.buffer:
                    line, _, rest = self.buffer.partition(b"\n")
                    self.buffer = bytearray(rest)
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("Invalid MCP response")
                    if "method" in value:
                        if "id" in value:
                            self._write(rpc_error(value["id"], -32601, "Upstream requests are disabled"))
                    elif value.get("id") == message["id"]:
                        return _response(value, message["id"])
            raise TimeoutError("MCP process response timed out")

    def close(self):
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=1)
        # Terminate descendants even if the command itself exited first.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.stdin.close()
        self.process.stdout.close()
        shutil.rmtree(self.home, ignore_errors=True)


class Bridge:
    def __init__(self, config):
        self.allowed = frozenset(config["enabled_tools"])
        self.upstream = StdioUpstream(config) if config["transport"] == "stdio" else HTTPUpstream(config)
        self.path = "/" + secrets.token_urlsafe(24)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.path != bridge.path:
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BYTES:
                        raise ValueError("Invalid request size")
                    message = json.loads(self.rfile.read(size))
                    result = bridge.dispatch(message)
                except (ValueError, TypeError):
                    result = rpc_error(None, -32600, "Invalid MCP request")
                body = json.dumps(result).encode() if result is not None else b""
                self.send_response(200 if result is not None else 202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def setup(self):
                super().setup()
                self.connection.settimeout(TIMEOUT)

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except Exception:
            self.upstream.close()
            raise
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return rpc_error(None, -32600, "Invalid MCP request")
        method, request_id = message.get("method"), message.get("id")
        if "id" not in message:
            if method not in NOTIFICATIONS:
                return None
        elif method not in METHODS:
            return rpc_error(request_id, -32601, "MCP method is not permitted")
        if method == "tools/call":
            params = message.get("params")
            if not isinstance(params, dict) or params.get("name") not in self.allowed:
                return rpc_error(request_id, -32602, "MCP tool is not permitted")
        try:
            response = self.upstream.request(message)
            if method == "tools/list" and response and "result" in response:
                result = response["result"]
                if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                    raise ValueError("Invalid MCP catalog")
                result["tools"] = [
                    tool
                    for tool in result["tools"]
                    if isinstance(tool, dict) and tool.get("name") in self.allowed
                ]
            return response
        except Exception:
            # Upstream exceptions can contain configured credentials or headers.
            return rpc_error(request_id, -32603, "MCP upstream failed") if "id" in message else None

    def replacement(self, config):
        return {
            "name": config["name"],
            "transport": "http",
            "url": f"http://127.0.0.1:{self.server.server_port}{self.path}",
            "headers": None,
            "enabled_tools": list(config["enabled_tools"]),
            "read_only": config["read_only"],
        }

    def close(self):
        self.upstream.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@contextmanager
def project_mcp(servers):
    """Replace project MCP servers with bounded, tool-filtered task-local endpoints."""
    with ExitStack() as stack:
        replacements = []
        for config in servers:
            bridge = Bridge(config)
            stack.callback(bridge.close)
            replacements.append(bridge.replacement(config))
        yield replacements
