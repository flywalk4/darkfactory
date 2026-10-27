"""Isolated, bounded wrappers around vendors' supported interactive login CLIs.

Run this module only inside a disposable auth container. Its stdout is a private
control pipe: a successful ``take_credentials`` response contains a secret.
Never forward that pipe into application logs or expose it directly to clients.
"""

import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

MAX_OUTPUT = 64 * 1024
LOGIN_TIMEOUT = 900
PASTE_SETTLE = 0.5
AUTHORIZATION_CODE = re.compile(r"[A-Za-z0-9_.~+/=-]+#[A-Za-z0-9_.~+/=-]+")
ANSI = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")
URL = re.compile(r"https://[^\s<>\"']+")
CLAUDE_TOKEN = re.compile(r"sk-ant-oat01-[A-Za-z0-9_-]{32,}(?=\s)")
CLAUDE_QUERY_KEYS = {
    "code",
    "client_id",
    "response_type",
    "redirect_uri",
    "scope",
    "code_challenge",
    "code_challenge_method",
    "state",
    "orgUUID",
    "login_hint",
    "login_method",
}
TERMINAL = {"done", "failed", "cancelled", "expired"}


class LoginFlow:
    def __init__(self, provider, directory, personal_mode=False, proxy_url=None):
        if provider not in {"codex", "claude"}:
            raise ValueError("Unsupported login provider")
        if provider == "codex" and not personal_mode:
            raise ValueError("Native Codex login requires personal deployment mode")
        self.provider = provider
        if proxy_url is not None:
            proxy = urlsplit(proxy_url)
            if (
                proxy.scheme != "http"
                or proxy.hostname != "127.0.0.1"
                or not proxy.port
                or proxy.username
                or proxy.password
                or proxy.path
                or proxy.query
                or proxy.fragment
            ):
                raise ValueError("Login proxy must be a controlled loopback endpoint")
        self._proxy_url = proxy_url
        self.directory = Path(directory).absolute()
        self._lock = threading.RLock()
        self._process = None
        self._master = None
        self._thread = None
        self._buffer = bytearray()
        self._credentials = None
        self._status = "created"
        self._url = None
        self._code = None
        self._rejected, self._retried = False, -1
        self._deadline = None

    def _command(self):
        if self.provider == "codex":
            return ["codex", "-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"]
        return ["claude", "setup-token"]

    def _environment(self):
        # Deliberately do not inherit API keys, OAuth tokens, vendor settings,
        # proxy configuration, loader hooks, or the host's credential paths.
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.directory),
            "CODEX_HOME": str(self.directory / "codex"),
            "CLAUDE_CONFIG_DIR": str(self.directory / "claude"),
            "XDG_CONFIG_HOME": str(self.directory / "config"),
            "XDG_CACHE_HOME": str(self.directory / "cache"),
            "TERM": "xterm-256color",
            "NO_COLOR": "1",
            "LANG": "C.UTF-8",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "BROWSER": "/bin/false",
        }
        if self._proxy_url:
            environment.update(
                HTTPS_PROXY=self._proxy_url,
                HTTP_PROXY=self._proxy_url,
                https_proxy=self._proxy_url,
                http_proxy=self._proxy_url,
                CLAUDE_CODE_PROXY_RESOLVES_HOSTS="1",
            )
        return environment

    def start(self):
        with self._lock:
            if self._status != "created":
                raise ValueError("Login flow already started")
            # A fresh directory is mandatory, including for tests. Never import
            # an existing ~/.codex or Claude configuration by accident.
            if self.directory.is_symlink():
                raise ValueError("Login directory must be isolated")
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if any(self.directory.iterdir()):
                raise ValueError("Login directory must be empty")
            self.directory.chmod(0o700)
            for name in ("codex", "claude", "config", "cache"):
                (self.directory / name).mkdir(mode=0o700)
            master, slave = pty.openpty()
            # Avoid wrapping long URLs/tokens into multiple terminal lines.
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 2000, 0, 0))
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            try:
                self._process = subprocess.Popen(
                    self._command(),
                    cwd=self.directory,
                    env=self._environment(),
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    start_new_session=True,
                )
            except Exception:
                os.close(master)
                self._status = "failed"
                return self.snapshot()
            finally:
                os.close(slave)
            self._master = master
            self._deadline = time.monotonic() + LOGIN_TIMEOUT
            self._status = "starting"
            self._thread = threading.Thread(target=self._watch, daemon=True)
            self._thread.start()
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            prompts = {
                "created": "Вход ещё не запущен",
                "starting": "Подготавливаем вход",
                "waiting": (
                    "Открой ссылку и введи код в браузере"
                    if self.provider == "codex"
                    else "Код не принят. Открой новую ссылку, разреши доступ и вставь новый код"
                    if self._rejected
                    else "Открой ссылку, разреши доступ и вставь полный код из браузера"
                ),
                "done": "Аккаунт подключён",
                "failed": "Вход не выполнен. Начни новую попытку",
                "cancelled": "Вход отменён",
                "expired": "Время входа истекло. Начни новую попытку",
            }
            return {
                "status": self._status,
                "verification_url": self._url,
                "user_code": self._code,
                "prompt": prompts[self._status],
            }

    def submit_code(self, code):
        # Claude's supported manual callback is authorization_code#state.
        if not isinstance(code, str) or not AUTHORIZATION_CODE.fullmatch(code):
            raise ValueError("Expected the complete browser code")
        if len(code) > 4096:
            raise ValueError("Browser code too long")
        with self._lock:
            if self.provider != "claude" or self._status != "waiting" or self._master is None:
                raise ValueError("Login is not waiting for a browser code")
            os.write(self._master, code.encode())
            self._rejected = False
        # Claude Code treats a long chunk as a paste and keeps a CR inside it as text,
        # so Enter must arrive as its own keystroke. In raw mode Enter is CR; LF never submits.
        time.sleep(PASTE_SETTLE)
        with self._lock:
            if self._status != "waiting" or self._master is None:
                raise ValueError("Login is not waiting for a browser code")
            os.write(self._master, b"\r")
        return self.snapshot()

    def _stop(self):
        if self._process is not None and self._process.poll() is None:
            try:
                self._signal(signal.SIGTERM)
                self._process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self._signal(signal.SIGKILL)
                self._process.wait(timeout=1)
            except ProcessLookupError:
                pass

    def _signal(self, number):
        try:
            os.killpg(self._process.pid, number)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Some host process supervisors restrict group signaling. Signal
            # our owned child directly; the enclosing container owns cleanup.
            if self._process.poll() is None:
                self._process.send_signal(number)

    def cancel(self):
        with self._lock:
            if self._status not in TERMINAL:
                self._status = "cancelled"
                self._stop()
                self._buffer.clear()
        return self.snapshot()

    def take_credentials(self):
        with self._lock:
            if self._status != "done" or self._credentials is None:
                return None
            credentials, self._credentials = self._credentials, None
            return credentials

    def _parse(self):
        text = ANSI.sub("", self._buffer.decode(errors="replace"))
        for match in URL.finditer(text):
            candidate = match.group().rstrip(").,;")
            parsed = urlsplit(candidate)
            if (
                parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or "sk-ant-" in candidate
                or len(candidate) > 8192
            ):
                continue
            valid = (
                self.provider == "codex"
                and parsed.hostname == "auth.openai.com"
                and parsed.path == "/codex/device"
                and not parsed.query
                and not parsed.fragment
            ) or (
                self.provider == "claude"
                and parsed.hostname in {"claude.ai", "claude.com"}
                and parsed.path in {"/oauth/authorize", "/cai/oauth/authorize"}
                and not parsed.fragment
                and all(key in CLAUDE_QUERY_KEYS for key, _ in parse_qsl(parsed.query))
            )
            if valid:
                self._url = candidate
                self._status = "waiting"
        if self.provider == "codex":
            match = re.search(r"one-time code[^\n]*\n\s*([A-Z0-9]{3,8}-[A-Z0-9]{3,8})\b", text)
            if match:
                self._code = match.group(1)
        else:
            rejected = text.rfind("OAuth error")
            if rejected > max(text.rfind("://"), self._retried):
                # A wrong or stale code: press Enter so Claude Code prints a fresh authorization link.
                self._retried, self._rejected = rejected, True
                os.write(self._master, b"\r")
            match = CLAUDE_TOKEN.search(text)
            if match:
                self._credentials = {"oauth_token": match.group()}
                self._status = "done"
                self._buffer.clear()
                self._stop()

    def _codex_credentials(self):
        path = self.directory / "codex" / "auth.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_OUTPUT:
            raise ValueError("Missing login result")
        value = json.loads(path.read_text())
        tokens = value.get("tokens", {})
        if not all(
            isinstance(tokens.get(key), str) and tokens[key] for key in ("access_token", "account_id")
        ):
            raise ValueError("Incomplete login result")
        result = {
            "auth_method": "codex_local",
            "accessToken": tokens["access_token"],
            "chatgptAccountId": tokens["account_id"],
        }
        if isinstance(tokens.get("refresh_token"), str) and tokens["refresh_token"]:
            result["refreshToken"] = tokens["refresh_token"]
        path.unlink()
        return result

    def _watch(self):
        try:
            while True:
                with self._lock:
                    if self._status in TERMINAL:
                        break
                    if time.monotonic() >= self._deadline:
                        self._status = "expired"
                        self._stop()
                        break
                ready, _, _ = select.select([self._master], [], [], 0.1)
                chunk = b""
                if ready:
                    try:
                        chunk = os.read(self._master, 8192)
                    except OSError:
                        pass
                with self._lock:
                    if self._status in TERMINAL:
                        break
                    if len(self._buffer) + len(chunk) > MAX_OUTPUT:
                        self._status = "failed"
                        self._buffer.clear()
                        self._stop()
                        break
                    self._buffer.extend(chunk)
                    self._parse()
                    if self._status == "done":
                        break
                    if self._process.poll() is not None and not chunk:
                        if self._process.returncode == 0 and self.provider == "codex":
                            self._credentials = self._codex_credentials()
                            self._status = "done"
                        else:
                            self._status = "failed"
                        break
        except Exception:
            with self._lock:
                self._status = "failed"
                self._credentials = None
                self._stop()
        finally:
            with self._lock:
                self._buffer.clear()
                if self._master is not None:
                    os.close(self._master)
                    self._master = None


def main():
    """Private JSON-lines control protocol; no raw vendor stdout ever escapes."""
    flow = None
    with tempfile.TemporaryDirectory(prefix="factory-login-") as directory:
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                    action = command.get("action")
                    if action == "start" and flow is None:
                        flow = LoginFlow(
                            command.get("provider"), directory, command.get("personal_mode") is True
                        )
                        response = flow.start()
                    elif flow is None:
                        raise ValueError("Login not started")
                    elif action in {"code", "submit_code"}:
                        response = flow.submit_code(command.get("code"))
                    elif action == "cancel":
                        response = flow.cancel()
                    elif action == "take_credentials":
                        credentials = flow.take_credentials()
                        response = (
                            {"status": "done", "credentials": credentials} if credentials else flow.snapshot()
                        )
                    elif action == "snapshot":
                        response = flow.snapshot()
                    else:
                        raise ValueError("Invalid command")
                except Exception:
                    response = {"status": "error", "prompt": "Недопустимая команда входа"}
                print(json.dumps(response, ensure_ascii=False), flush=True)
        finally:
            if flow is not None:
                flow.cancel()


if __name__ == "__main__":
    main()
