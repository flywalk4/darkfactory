"""Real local bridge requests prove MCP permissions before upstream execution."""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from darkfactory import mcp_bridge


def config(**values):
    return {
        "name": "project",
        "transport": "http",
        "url": "https://mcp.invalid/rpc",
        "headers": {"Authorization": "Bearer private-upstream-token"},
        "enabled_tools": ["read"],
        "read_only": True,
        **values,
    }


def rpc(url, method, request_id=1, params=None):
    message = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return httpx.post(url, json=message, trust_env=False, timeout=3)


def fake_http(monkeypatch, handler):
    client_type = httpx.Client
    options = []

    def factory(**kwargs):
        options.append(kwargs)
        return client_type(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mcp_bridge, "HTTP_CLIENT", factory)
    return options


def test_http_catalog_and_calls_enforce_exact_tools_and_preserve_session(monkeypatch):
    requests = []

    def upstream(request):
        message = json.loads(request.content)
        requests.append((message, dict(request.headers)))
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"MCP-Session-Id": "upstream-session"},
            )
        if "id" not in message:
            return httpx.Response(202)
        result = {"tools": [{"name": "read"}, {"name": "write"}, {"name": "read_all"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    options = fake_http(monkeypatch, upstream)
    with mcp_bridge.project_mcp([config()]) as servers:
        replacement = servers[0]
        assert replacement["headers"] is None
        assert "private-upstream-token" not in json.dumps(replacement)
        assert replacement["name"] == "project" and replacement["read_only"] is True
        assert replacement["enabled_tools"] == ["read"]
        url = replacement["url"]
        assert rpc(url, "initialize").json()["result"] == {"protocolVersion": "2025-06-18"}
        assert rpc(url, "notifications/initialized", None).status_code == 202
        assert rpc(url, "tools/list", 2).json()["result"]["tools"] == [{"name": "read"}]
        before = len(requests)
        for name in ("write", "read_all", "READ"):
            assert rpc(url, "tools/call", 3, {"name": name}).json()["error"]["code"] == -32602
        assert len(requests) == before
        assert "result" in rpc(url, "tools/call", 4, {"name": "read"}).json()
        assert rpc(url, "sampling/createMessage", 5).json()["error"]["code"] == -32601
        assert requests[-1][0]["id"] == 4
        assert requests[1][1]["mcp-session-id"] == "upstream-session"
        assert requests[1][1]["mcp-protocol-version"] == "2025-06-18"
        assert all(item[1]["authorization"] == "Bearer private-upstream-token" for item in requests)
    assert options == [{"timeout": 30, "follow_redirects": False, "trust_env": True}]
    with pytest.raises(httpx.ConnectError):
        rpc(url, "ping")


def test_http_sse_and_error_messages_do_not_surface_credentials(monkeypatch):
    def upstream(request):
        message = json.loads(request.content)
        if message["method"] == "ping":
            raise RuntimeError("private-upstream-token")
        body = (
            "event: message\r\ndata: "
            + json.dumps(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": [{"name": "read"}]}},
            )
            + "\r\n\r\n"
        )
        return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

    fake_http(monkeypatch, upstream)
    with mcp_bridge.project_mcp([config()]) as servers:
        url = servers[0]["url"]
        assert rpc(url, "tools/list").json()["result"]["tools"] == [{"name": "read"}]
        failed = rpc(url, "ping").json()
        assert failed["error"]["code"] == -32603
        assert "private-upstream-token" not in json.dumps(failed)


@pytest.mark.parametrize("mode", ["oversized", "redirect", "server_request"])
def test_http_rejects_oversize_redirects_and_upstream_requests(monkeypatch, mode):
    def upstream(request):
        if mode == "oversized":
            return httpx.Response(200, content=b" " * (mcp_bridge.MAX_BYTES + 1))
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://other.invalid"})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage", "params": {}}
        )

    fake_http(monkeypatch, upstream)
    with mcp_bridge.project_mcp([config()]) as servers:
        assert rpc(servers[0]["url"], "ping").json()["error"]["code"] == -32603


def test_stdio_real_process_initialization_catalog_env_and_shutdown(monkeypatch, tmp_path):
    script = tmp_path / "mcp.py"
    script.write_text("""import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"initialized": True}
    elif message["method"] == "tools/list":
        result = {"tools": [{"name": "read"}, {"name": "write"}]}
    else:
        home = os.environ["HOME"]
        assert os.getcwd() == os.path.realpath(home)
        open(os.path.join(home, "cache"), "w").close()
        result = {"configured": os.environ.get("CUSTOM_MCP"), "home": home,
                  "leaked": [key for key in ("CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
                             if key in os.environ]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""")
    for key in ("CODEX_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(key, "private-parent-credential")
    # The runner's own HOME belongs to root; the server gets a writable one of its own.
    monkeypatch.setenv("HOME", str(tmp_path / "runner-home"))
    processes = []
    original = mcp_bridge.subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(mcp_bridge.subprocess, "Popen", capture)
    server = config(
        transport="stdio",
        command=sys.executable,
        args=[str(script)],
        env={"CUSTOM_MCP": "explicit"},
        url=None,
        headers=None,
    )
    with mcp_bridge.project_mcp([server]) as servers:
        url = servers[0]["url"]
        assert rpc(url, "initialize").json()["result"] == {"initialized": True}
        assert rpc(url, "tools/list", 2).json()["result"]["tools"] == [{"name": "read"}]
        assert rpc(url, "tools/call", 3, {"name": "write"}).json()["error"]["code"] == -32602
        result = rpc(url, "tools/call", 4, {"name": "read"}).json()["result"]
        home = result.pop("home")
        assert result == {"configured": "explicit", "leaked": []}
        assert home != str(tmp_path / "runner-home") and os.path.isfile(os.path.join(home, "cache"))
        assert processes[0].poll() is None
    assert processes[0].poll() is not None
    assert not os.path.exists(home)
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_stdio_cancellation_notification_reaches_process_and_tears_down(tmp_path):
    marker = tmp_path / "cancelled"
    script = tmp_path / "cancel.py"
    script.write_text("""import json, pathlib, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "notifications/cancelled":
        pathlib.Path(sys.argv[1]).write_text("cancelled")
        print(json.dumps({"jsonrpc":"2.0", "id":1, "result":{}}), flush=True)
""")
    server = config(
        transport="stdio",
        command=sys.executable,
        args=[str(script), str(marker)],
        env={},
        url=None,
        headers=None,
    )
    with mcp_bridge.project_mcp([server]) as servers, ThreadPoolExecutor(max_workers=1) as executor:
        url = servers[0]["url"]
        pending = executor.submit(rpc, url, "tools/call", 1, {"name": "read"})
        # Cancellation must bypass the serialized response wait.
        assert rpc(url, "notifications/cancelled", None, {"requestId": 1}).status_code == 202
        assert pending.result(timeout=3).json()["result"] == {}
        assert marker.read_text() == "cancelled"


def test_stdio_rejects_server_sampling_request_before_matching_result(tmp_path):
    script = tmp_path / "sampling.py"
    script.write_text("""import json, sys
message = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0", "id":"server", "method":"sampling/createMessage"}), flush=True)
denied = json.loads(sys.stdin.readline())
print(json.dumps({"jsonrpc":"2.0", "id":message["id"], "result":{"denied":denied["error"]["code"]}}), flush=True)
""")
    server = config(transport="stdio", command=sys.executable, args=[str(script)], url=None, headers=None)
    with mcp_bridge.project_mcp([server]) as servers:
        assert rpc(servers[0]["url"], "initialize").json()["result"] == {"denied": -32601}


def test_context_closes_earlier_bridge_if_later_upstream_fails(monkeypatch):
    closed = threading.Event()
    original_close = mcp_bridge.HTTPUpstream.close

    def close(self):
        original_close(self)
        closed.set()

    monkeypatch.setattr(mcp_bridge.HTTPUpstream, "close", close)
    with pytest.raises(ValueError, match="HTTPS"):
        with mcp_bridge.project_mcp([config(), config(url="http://untrusted.invalid")]):
            pass
    assert closed.is_set()


def test_stdio_write_timeout_is_bounded_and_process_closes(monkeypatch):
    monkeypatch.setattr(mcp_bridge, "TIMEOUT", 0.1)
    upstream = mcp_bridge.StdioUpstream(
        config(transport="stdio", command=sys.executable, args=["-c", "import time; time.sleep(100)"])
    )
    try:
        with pytest.raises(TimeoutError, match="write timed out"):
            upstream.request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": "x" * 500000})
    finally:
        upstream.close()
    assert upstream.process.poll() is not None


def test_stdio_read_timeout_is_bounded_and_process_closes(monkeypatch):
    monkeypatch.setattr(mcp_bridge, "TIMEOUT", 0.1)
    upstream = mcp_bridge.StdioUpstream(
        config(transport="stdio", command=sys.executable, args=["-c", "import time; time.sleep(100)"])
    )
    try:
        with pytest.raises(TimeoutError, match="response timed out"):
            upstream.request({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    finally:
        upstream.close()
    assert upstream.process.poll() is not None
