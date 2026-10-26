"""Run inside the Compose worker; real Docker/Git pipeline, deterministic HTTPS model fixture.

docker compose exec -T worker python - < scripts/docker_smoke.py
No real model calls or paid credentials. Leaves isolated test organization and repositories for inspection.
"""

import base64
import datetime
import http.server
import io
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import docker
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID

from darkfactory.db import encrypt
from darkfactory.gitops import GitError, Repository

run_id = uuid.uuid4().hex[:8]
root = Path("/repos") / ("smoke-" + run_id)
root.mkdir()
fixture_calls = {"plan": 0, "implement": 0, "review": 0}
observed_containers = set()
ssh_enabled = os.getenv("SMOKE_SSH") == "1"


def ssh_fixture(client, cleanup):
    worker = client.containers.get(os.environ["HOSTNAME"])
    network = next(iter(worker.attrs["NetworkSettings"]["Networks"]))
    volume = next(m["Name"] for m in worker.attrs["Mounts"] if m["Destination"] == "/repos")
    name = "darkfactory-smoke-ssh-" + run_id
    host_key, identity = (ed25519.Ed25519PrivateKey.generate() for _ in range(2))

    def private(key):
        return key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
        ).decode()

    def public(key):
        return (
            key.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )

    files = {
        "host_key": private(host_key),
        "authorized_keys": public(identity) + "\n",
        "sshd_config": "Port 22\nHostKey /fixture/host_key\nAuthorizedKeysFile /fixture/authorized_keys\n"
        "PermitRootLogin prohibit-password\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "UsePAM no\nAllowUsers root\nAllowTcpForwarding no\nPermitTunnel no\nX11Forwarding no\n"
        'ForceCommand /usr/bin/git-shell -c "$SSH_ORIGINAL_COMMAND"\n',
    }
    container = client.containers.create(
        "darkfactory-smoke-ssh:local",
        name=name,
        network=network,
        volumes={volume: {"bind": "/repos", "mode": "rw"}},
        labels={"darkfactory.smoke": run_id},
        tmpfs={"/run": "rw,nosuid,noexec,size=4m", "/tmp": "rw,nosuid,noexec,size=4m"},
    )
    cleanup.callback(container.remove, force=True)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for path, content in files.items():
            item = tarfile.TarInfo("fixture/" + path)
            payload = content.encode()
            item.size, item.mode = len(payload), 0o600
            bundle.addfile(item, io.BytesIO(payload))
    container.put_archive("/", archive.getvalue())
    container.start()
    deadline = time.monotonic() + 15
    while True:
        try:
            with socket.create_connection((name, 22), timeout=1):
                break
        except OSError:
            container.reload()
            if container.status != "running" or time.monotonic() >= deadline:
                raise AssertionError(container.logs().decode()) from None
            time.sleep(0.1)
    return (
        name,
        {"ssh_private_key": private(identity), "ssh_known_hosts": f"{name} {public(host_key)}"},
        private,
        public,
    )


def verify_ssh(remote, credentials, private, public):
    for failure in ("host_key", "identity", None):
        values = dict(credentials)
        if failure == "host_key":
            values["ssh_known_hosts"] = (
                remote.split("/")[2].split("@")[1] + " " + public(ed25519.Ed25519PrivateKey.generate())
            )
        elif failure == "identity":
            values["ssh_private_key"] = private(ed25519.Ed25519PrivateKey.generate())
        repo = Repository(
            SimpleNamespace(id=uuid.uuid4().hex, secret=encrypt(json.dumps(values))),
            SimpleNamespace(id=uuid.uuid4().hex),
        )
        try:
            refs = repo.git("ls-remote", remote)
        except GitError as exc:
            expected = "Host key verification failed" if failure == "host_key" else "Permission denied"
            assert failure and expected in str(exc), str(exc)
        else:
            assert failure is None and "refs/heads/main" in refs
    print("SSH: wrong host key rejected; wrong identity rejected; valid identity accepted", flush=True)


def git(*args, cwd=None):
    return subprocess.check_output(
        ["git", "-c", "user.name=Smoke", "-c", "user.email=smoke@localhost", *args],
        cwd=cwd,
        stderr=subprocess.PIPE,
        text=True,
    ).strip()


class ModelFixture(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = data["messages"]
        prompt = messages[1]["content"]
        review = prompt.startswith("Independently review commit")
        if prompt.startswith("Plan task before implementation"):
            fixture_calls["plan"] += 1
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "plan": "Inspect affected module, apply requested behavior, add regression tests, run project checks.",
                        "questions": [],
                        "subtasks": [],
                    }
                ),
            }
        elif messages[-1]["role"] == "tool":
            output = messages[-1]["content"]
            approved = "exit=0\n" in output
            text = (
                json.dumps({"approved": approved, "findings": [] if approved else [output]})
                if review
                else "Fixture applied requested patch."
            )
            message = {"role": "assistant", "content": text}
        else:
            specification = json.loads(
                base64.b64decode(re.search(r"SMOKE_SPEC:([A-Za-z0-9+/=]+)", prompt)[1])
            )
            if review:
                # Execute an independent inspection inside the read-only review container.
                command = "git diff --stat HEAD~1 HEAD; python -m unittest discover -s tests -v"
            else:
                code = "from pathlib import Path\n"
                for name, content in specification.items():
                    code += f"Path({name!r}).write_text({content!r})\n"
                command = "python -c " + shlex.quote(code)
            fixture_calls["review" if review else "implement"] += 1
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": uuid.uuid4().hex,
                        "type": "function",
                        "function": {"name": "shell", "arguments": json.dumps({"command": command})},
                    }
                ],
            }
        payload = json.dumps(
            {"choices": [{"message": message}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Darkfactory smoke fixture")])
now = datetime.datetime.now(datetime.UTC)
cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(subject)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - datetime.timedelta(minutes=1))
    .not_valid_after(now + datetime.timedelta(days=1))
    .add_extension(x509.SubjectAlternativeName([x509.DNSName("worker")]), critical=False)
    .sign(key, hashes.SHA256())
)
certificate = cert.public_bytes(serialization.Encoding.PEM).decode()
with ExitStack() as cleanup, tempfile.TemporaryDirectory() as temporary:
    certificate_path, key_path = Path(temporary) / "cert.pem", Path(temporary) / "key.pem"
    certificate_path.write_text(certificate)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    os.chmod(key_path, 0o600)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), ModelFixture)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cleanup.callback(server.server_close)
    cleanup.callback(server.shutdown)
    client = docker.from_env()
    cleanup.callback(client.close)
    ssh = ssh_fixture(client, cleanup) if ssh_enabled else None

    api = httpx.Client(
        base_url="http://app:8000/api",
        timeout=20,
        headers={
            "Authorization": "Bearer "
            + (os.getenv("DARKFACTORY_ADMIN_TOKEN") or Path("/data/bootstrap-token").read_text()).strip()
        },
    )
    cleanup.callback(api.close)

    def request(method, path, **kwargs):
        response = api.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    organization = request("POST", "/organizations", json={"name": "Docker smoke " + run_id})
    api.headers["Authorization"] = "Bearer " + organization["token"]
    token_path = Path("/data/smoke-token")
    token_path.write_text(organization["token"])
    token_path.chmod(0o600)
    pool = request("POST", "/pools", json={"name": "Local fixture — no real model", "cross_runtime": False})
    request(
        "POST",
        "/connections",
        json={
            "name": "HTTPS fixture",
            "provider": "api",
            "account_key": "smoke-" + run_id,
            "pool_ids": [pool["id"]],
            "model": "deterministic-fixture",
            "credentials": {
                "api_key": "fixture-" + run_id,
                "base_url": f"https://worker:{server.server_port}/v1",
                "ca_cert": certificate,
                "input_usd_per_million": 0,
                "output_usd_per_million": 0,
            },
        },
    )
    projects, tasks, original_heads = [], [], {}
    for project_number in (1, 2):
        work, bare = root / f"source-{project_number}", root / f"project-{project_number}.git"
        work.mkdir()
        (work / "tests").mkdir()
        for bug in range(1, 6):
            (work / f"calculator{bug}.py").write_text("def double(n):\n    return n * 2 if n >= 0 else n\n")
        (work / "tests/test_common.py").write_text(
            "import unittest\n"
            + "\n".join(f"from calculator{i} import double as double{i}" for i in range(1, 6))
            + "\nclass Common(unittest.TestCase):\n    def test_positive(self):\n"
            + "".join(f"        self.assertEqual(double{i}(2), 4)\n" for i in range(1, 6))
        )
        git("init", "-b", "main", cwd=work)
        git("add", ".", cwd=work)
        git("commit", "-m", "Seed test project", cwd=work)
        git("clone", "--bare", str(work), str(bare))
        original_heads[str(bare)] = git("rev-parse", "main", cwd=bare)
        remote, git_credentials = str(bare), {}
        if ssh and project_number == 2:
            host, git_credentials, private, public = ssh
            remote = f"ssh://root@{host}{bare}"
            verify_ssh(remote, git_credentials, private, public)
        project = request(
            "POST",
            "/projects",
            json={
                "name": f"Smoke {run_id} / {project_number}",
                "remote": remote,
                **git_credentials,
                "writer": "api",
                "reviewer": "api",
                "writer_pool": pool["id"],
                "reviewer_pool": pool["id"],
                "checks": ["python -m unittest discover -s tests -v"],
            },
        )
        projects.append(project)
        for bug in range(1, 6):
            spec = {
                f"calculator{bug}.py": "def double(n):\n    return n * 2\n",
                f"tests/test_negative{bug}.py": f"import unittest\nfrom calculator{bug} import double\nclass Negative(unittest.TestCase):\n    def test_negative(self):\n        self.assertEqual(double(-2), -4)\n",
            }
            task = request(
                "POST",
                f"/projects/{project['id']}/tasks",
                json={
                    "title": f"Bug {project_number}-{bug}: negative double",
                    "request": "Fix negative numbers and add regression test. SMOKE_SPEC:"
                    + base64.b64encode(json.dumps(spec).encode()).decode(),
                    "explicit_api": True,
                    "api_budget_usd": 1,
                },
            )
            tasks.append(task)
        for feature in range(1, 3 if project_number == 1 else 2):
            spec = {
                f"feature{feature}.py": "def triple(n):\n    return n * 3\n",
                f"tests/test_feature{feature}.py": f"import unittest\nfrom feature{feature} import triple\nclass Feature(unittest.TestCase):\n    def test_triple(self):\n        self.assertEqual(triple(7), 21)\n",
            }
            tasks.append(
                request(
                    "POST",
                    f"/projects/{project['id']}/tasks",
                    json={
                        "title": f"Feature {project_number}-{feature}: triple",
                        "request": "Add triple and its regression test. SMOKE_SPEC:"
                        + base64.b64encode(json.dumps(spec).encode()).decode(),
                        "explicit_api": True,
                        "api_budget_usd": 1,
                    },
                )
            )

    deadline = time.monotonic() + 240
    previous = None
    while time.monotonic() < deadline:
        for container in client.containers.list(filters={"label": "darkfactory.task"}):
            attrs = container.attrs
            host = attrs["HostConfig"]
            assert host["NetworkMode"] == "none", host
            assert host["ReadonlyRootfs"] and "ALL" in host["CapDrop"]
            assert all(
                mount["Destination"] not in {"/var/run/docker.sock", "/repos", "/data"}
                for mount in attrs["Mounts"]
            )
            observed_containers.add(container.id)
        states = [request("GET", "/tasks/" + task["id"]) for task in tasks]
        progress = sum(task["state"] == "merged" for task in states)
        if previous != progress:
            print(
                f"Merged {progress}/13; isolated containers observed: {len(observed_containers)}", flush=True
            )
            previous = progress
        failed = [
            (task["title"], task["state"], task["phase"], task["reason"])
            for task in states
            if task["state"] in {"needs_human", "cancelled", "review_required"}
        ]
        assert not failed, failed
        if progress == 13:
            break
        time.sleep(1)
    else:
        raise AssertionError(
            [(task["title"], task["state"], task["phase"], task["reason"]) for task in states]
        )
    for task in states:
        assert task["result_sha"] == task["verified_sha"] == task["reviewed_sha"]
    for bare, main_sha in original_heads.items():
        assert git("rev-parse", "main", cwd=bare) == main_sha
        assert git("rev-parse", "preprod", cwd=bare) != main_sha
    assert fixture_calls["implement"] >= 13 and fixture_calls["review"] >= 13
    assert observed_containers
    if ssh:
        # The test SSH server is temporary; do not schedule later checks against it.
        project = projects[1]
        for _ in range(30):
            response = api.put("/projects/" + project["id"], json={**project, "checks": []})
            if response.status_code != 409:
                assert response.status_code == 200, response.text
                break
            time.sleep(1)
        else:
            raise AssertionError("Could not disable monitoring of the temporary SSH fixture")
    print(
        json.dumps(
            {
                "result": "PASS",
                "tasks": 13,
                "bugs": 10,
                "features": 3,
                "projects": 2,
                "real_git": True,
                "git_transports": ["local", "ssh"] if ssh else ["local"],
                "ssh_host_key_and_identity_rejected": bool(ssh),
                "real_task_containers": len(observed_containers),
                "model": "deterministic HTTPS fixture, no real AI",
                "main_unchanged": True,
                "fixture_calls": fixture_calls,
                "repositories": str(root),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
