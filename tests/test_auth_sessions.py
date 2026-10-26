import hashlib
import json
import os
from contextlib import nullcontext

import docker
import pytest
from sqlalchemy import select

from darkfactory import auth_sessions as auth
from darkfactory.db import Connection, Pool, Session, decrypt, encrypt, now, transaction, uid


class FakeContainer:
    def __init__(self, manager, name):
        self.manager, self.name = manager, name
        self.id, self.status = uid(), "created"
        self.starts = 0

    def reload(self):
        pass

    def start(self):
        self.starts += 1
        self.status = "running"

    def remove(self, force=False):
        assert force is True
        self.manager.items.pop(self.name, None)
        self.status = "removed"


class FakeContainers:
    def __init__(self):
        self.items, self.calls = {}, []

    def get(self, identity):
        for container in self.items.values():
            if identity in {container.id, container.name}:
                return container
        raise docker.errors.NotFound("Not found")

    def create(self, image, command=None, **kwargs):
        self.calls.append({"image": image, "command": command, **kwargs})
        container = FakeContainer(self, kwargs["name"])
        self.items[container.name] = container
        return container


class FakeDocker:
    def __init__(self):
        self.containers = FakeContainers()


@pytest.fixture
def controller(monkeypatch, setup_project):
    monkeypatch.setattr(auth, "egress_proxy", lambda *args: nullcontext())
    monkeypatch.setenv("DARKFACTORY_PERSONAL_MODE", "1")
    fake = FakeDocker()
    yield fake
    for context in auth._PROXIES.values():
        context.__exit__(None, None, None)
    auth._PROXIES.clear()


def login(setup_project, provider="claude", account="person@example.invalid", **extra):
    _, pool = setup_project
    with transaction() as db:
        row = auth.LoginSession(
            org_id=db.get(Pool, pool["id"]).org_id,
            provider=provider,
            pool_ids=[pool["id"]],
            name="Guided account",
            account_key=account,
            **extra,
        )
        db.add(row)
        db.flush()
        return row.id


def row(identity):
    with Session() as db:
        return db.get(auth.LoginSession, identity)


def test_container_isolated_and_only_one_global_login_starts(controller, setup_project):
    identity = login(setup_project)
    login(setup_project, account="second@example.invalid")
    auth.advance_logins(controller)
    auth.advance_logins(controller)
    assert len(controller.containers.calls) == 1
    call = controller.containers.calls[0]
    assert call["network_mode"] == "none"
    # The executor image's entrypoint is the task runner; the login container must replace it.
    assert call["entrypoint"] == ["python", "-m", "darkfactory.auth_sessions", "--runner"]
    assert call["read_only"] is True
    assert call["user"] == "1000:1000"
    assert call["cap_drop"] == ["ALL"]
    assert call["pids_limit"] == 128 and call["mem_limit"] == "512m"
    assert call["log_config"]["Type"] == "none"
    mount = call["mounts"][0]
    assert mount["Target"] == "/run/auth"
    assert mount["VolumeOptions"]["Subpath"] == "auth/" + identity
    assert len(call["mounts"]) == 1
    directory = auth._directory(row(identity))
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert set(json.loads((directory / "job.json").read_text())) == {"provider", "personal_mode"}


def test_snapshots_redact_raw_output_and_result_is_encrypted(controller, setup_project):
    identity = login(setup_project)
    auth.advance_logins(controller)
    directory = auth._directory(row(identity))
    (directory / "state.json").write_text(
        json.dumps(
            {
                "status": "waiting",
                "verification_url": "https://claude.com/cai/oauth/authorize?state=fake",
                "prompt": "secret-token",
                "user_code": "secret-token",
                "oauth_token": "secret-token",
            }
        )
    )
    auth.advance_logins(controller)
    current = row(identity)
    assert current.status == "waiting"
    assert "secret-token" not in json.dumps(auth.public_login(current))
    assert "secret-token" not in json.dumps(current.snapshot)
    credentials = {"oauth_token": "fake-issued-token"}
    (directory / "result.json").write_text(json.dumps(credentials))
    auth.advance_logins(controller)
    current = row(identity)
    assert current.status == "done" and current.connection_id
    with Session() as db:
        connection = db.get(Connection, current.connection_id)
        assert "fake-issued-token" not in connection.secret
        assert json.loads(decrypt(connection.secret)) == credentials
    assert "fake-issued-token" not in json.dumps(auth.public_login(current))
    assert not directory.exists()
    assert not controller.containers.items


def test_recovery_attaches_existing_container_without_second_start(controller, setup_project):
    identity = login(setup_project)
    auth.advance_logins(controller)
    with transaction() as db:
        current = db.get(auth.LoginSession, identity)
        current.container_id = None
        current.controller_id, current.lease_until = "dead-controller", 0
    auth._PROXIES.clear()
    auth.advance_logins(controller)
    assert len(controller.containers.calls) == 1
    assert row(identity).container_id
    assert next(iter(controller.containers.items.values())).starts == 1


def test_code_moves_from_encrypted_database_to_private_command_file(controller, setup_project):
    identity = login(setup_project)
    auth.advance_logins(controller)
    command = {"action": "code", "code": "fake-code#fake-state"}
    with transaction() as db:
        current = db.get(auth.LoginSession, identity)
        current.command_secret = encrypt(json.dumps(command))
    auth.advance_logins(controller)
    current = row(identity)
    assert current.command_secret is not None
    path = auth._directory(current) / "command.json"
    payload = json.loads(path.read_text())
    assert {key: value for key, value in payload.items() if key != "id"} == command
    assert len(payload["id"]) == 64
    assert (path.stat().st_mode & 0o777) == 0o600
    assert "fake-code" not in json.dumps(auth.public_login(current))


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_expiry_and_cancel_remove_container_and_private_files(controller, setup_project, status):
    identity = login(setup_project)
    auth.advance_logins(controller)
    directory = auth._directory(row(identity))
    (directory / "result.json").write_text('{"oauth_token":"must-not-import"}')
    with transaction() as db:
        current = db.get(auth.LoginSession, identity)
        current.command_secret = encrypt('{"action":"code","code":"secret#state"}')
        if status == "cancelled":
            current.status = status
        else:
            current.expires = now() - 1
    auth.advance_logins(controller)
    current = row(identity)
    assert current.status == status and current.connection_id is None
    assert current.command_secret is None
    assert not directory.exists() and not controller.containers.items


def test_codex_personal_gate_and_stable_account_identity(controller, setup_project, monkeypatch):
    identity = login(setup_project, provider="codex", account="user-entered-label")
    auth.advance_logins(controller)
    directory = auth._directory(row(identity))
    credentials = {
        "auth_method": "codex_local",
        "accessToken": "fake-access",
        "chatgptAccountId": "stable-codex-account",
    }
    (directory / "result.json").write_text(json.dumps(credentials))
    auth.advance_logins(controller)
    with Session() as db:
        connection = db.get(Connection, row(identity).connection_id)
        assert connection.account_key == hashlib.sha256(b"stable-codex-account").hexdigest()
    blocked = login(setup_project, provider="codex")
    monkeypatch.delenv("DARKFACTORY_PERSONAL_MODE")
    count = len(controller.containers.calls)
    auth.advance_logins(controller)
    assert row(blocked).status == "failed"
    assert len(controller.containers.calls) == count


@pytest.mark.parametrize("leased", [False, True])
def test_same_account_reconnect_or_reject_active_lease(controller, setup_project, leased):
    identity = login(setup_project)
    with transaction() as db:
        current = db.get(auth.LoginSession, identity)
        connection = Connection(
            org_id=current.org_id,
            provider="claude",
            name="Existing",
            account_key=hashlib.sha256(current.account_key.encode()).hexdigest(),
            pool_ids=[],
            secret=encrypt('{"oauth_token":"old-token"}'),
            lease_task="active-task" if leased else None,
        )
        db.add(connection)
        db.flush()
        connection_id = connection.id
    auth.advance_logins(controller)
    directory = auth._directory(row(identity))
    (directory / "result.json").write_text('{"oauth_token":"new-token"}')
    auth.advance_logins(controller)
    current = row(identity)
    assert current.status == ("failed" if leased else "done")
    with Session() as db:
        connection = db.get(Connection, connection_id)
        assert json.loads(decrypt(connection.secret))["oauth_token"] == (
            "old-token" if leased else "new-token"
        )
        if not leased:
            assert connection.pool_ids == current.pool_ids
            assert current.connection_id == connection_id


def test_cross_organization_account_is_not_reconnected(controller, setup_project):
    from darkfactory.db import Organization

    identity = login(setup_project)
    with transaction() as db:
        other = Organization(name="Other org")
        db.add(other)
        db.flush()
        current = db.get(auth.LoginSession, identity)
        db.add(
            Connection(
                org_id=other.id,
                provider="claude",
                name="Other account",
                account_key=hashlib.sha256(current.account_key.encode()).hexdigest(),
                pool_ids=[],
                secret=encrypt('{"oauth_token":"other-secret"}'),
            )
        )
    auth.advance_logins(controller)
    directory = auth._directory(row(identity))
    (directory / "result.json").write_text('{"oauth_token":"new-token"}')
    auth.advance_logins(controller)
    assert row(identity).status == "failed"
    with Session() as db:
        connection = db.scalar(select(Connection))
        assert json.loads(decrypt(connection.secret))["oauth_token"] == "other-secret"


def test_file_runner_uses_explicit_proxy_and_never_stdout(tmp_path, monkeypatch, capsys):
    auth._write_private(tmp_path / "job.json", {"provider": "claude", "personal_mode": False})
    auth._write_private(tmp_path / "command.json", {"id": "a" * 64, "action": "code", "code": "late#code"})
    received = {}

    class DoneFlow:
        def __init__(self, provider, home, personal_mode=False, proxy_url=None):
            received.update(provider=provider, home=home, proxy=proxy_url)

        def start(self):
            pass

        def snapshot(self):
            return {"status": "done"}

        def take_credentials(self):
            return {"oauth_token": "fake-private-token"}

        def submit_code(self, code):
            pytest.fail("Successful login must ignore late code")

        def cancel(self):
            pass

    monkeypatch.setattr(auth, "LoginFlow", DoneFlow)
    monkeypatch.setattr(auth, "local_proxy", lambda socket: nullcontext("http://127.0.0.1:9999"))
    auth.file_runner(tmp_path)
    assert received["proxy"] == "http://127.0.0.1:9999"
    assert received["home"] != str(tmp_path)
    assert not os.path.exists(received["home"])
    assert json.loads((tmp_path / "result.json").read_text()) == {"oauth_token": "fake-private-token"}
    assert (tmp_path / "result.json").stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""


def test_connection_commits_before_private_result_cleanup(controller, setup_project, monkeypatch):
    identity = login(setup_project)
    auth.advance_logins(controller)
    directory = auth._directory(row(identity))
    (directory / "result.json").write_text('{"oauth_token":"durable-token"}')
    cleanup = auth._cleanup
    observed = []

    def verify_commit(client, current):
        if current.status == "done":
            with Session() as db:
                durable = db.get(auth.LoginSession, current.id)
                assert durable.status == "done"
                assert db.get(Connection, durable.connection_id) is not None
                observed.append(True)
        cleanup(client, current)

    monkeypatch.setattr(auth, "_cleanup", verify_commit)
    auth.advance_logins(controller)
    assert observed == [True]


def test_pending_command_never_rewritten_and_ack_survives_controller_restart(
    controller, setup_project, monkeypatch
):
    identity = login(setup_project)
    auth.advance_logins(controller)
    with transaction() as db:
        current = db.get(auth.LoginSession, identity)
        current.command_secret = encrypt('{"action":"code","code":"deliver-once#state"}')
    writes = []
    writer = auth._write_private

    def record_write(path, value, **kwargs):
        if path.name == "command.json":
            writes.append(value)
        writer(path, value, **kwargs)

    monkeypatch.setattr(auth, "_write_private", record_write)
    auth.advance_logins(controller)
    directory = auth._directory(row(identity))
    payload = json.loads((directory / "command.json").read_text())
    auth.advance_logins(controller)
    assert len(writes) == 1 and row(identity).command_secret
    with transaction() as db:
        current = db.get(auth.LoginSession, identity)
        current.controller_id, current.lease_until = "previous-worker", 0
    auth._PROXIES.clear()
    auth.advance_logins(controller)
    assert len(writes) == 1 and row(identity).command_secret
    # Simulate runner's durable claim, then a controller crash before ack.
    writer(directory / "claimed.json", {"id": payload["id"]})
    (directory / "command.json").unlink()
    auth.advance_logins(controller)
    assert len(writes) == 1 and row(identity).command_secret
    # An unrelated or stale ack must not release API's pending-command guard.
    writer(directory / "ack.json", {"id": "0" * 64})
    auth.advance_logins(controller)
    assert len(writes) == 1 and row(identity).command_secret
    writer(directory / "ack.json", {"id": payload["id"]})
    with transaction() as db:
        current = db.get(auth.LoginSession, identity)
        current.controller_id, current.lease_until = "previous-worker", 0
    auth.advance_logins(controller)
    assert len(writes) == 1 and row(identity).command_secret is None
    assert not (directory / "command.json").exists()


def test_runner_ack_prevents_duplicate_code_delivery_and_contains_only_id(tmp_path):
    payload = {"id": "b" * 64, "action": "code", "code": "private-callback#state"}

    class WaitingFlow:
        calls = []

        def submit_code(self, code):
            self.calls.append(code)

    flow = WaitingFlow()
    auth._write_private(tmp_path / "command.json", payload)
    auth._consume_command(flow, tmp_path)
    assert flow.calls == [payload["code"]]
    assert json.loads((tmp_path / "ack.json").read_text()) == {"id": payload["id"]}
    assert json.loads((tmp_path / "claimed.json").read_text()) == {"id": payload["id"]}
    assert payload["code"] not in (tmp_path / "ack.json").read_text()
    # Controller recovery cannot replay an acknowledged command, even if its
    # original delivery file reappears before the database commit completes.
    auth._write_private(tmp_path / "command.json", payload)
    auth._consume_command(flow, tmp_path)
    assert flow.calls == [payload["code"]]
    assert not (tmp_path / "command.json").exists()


def test_interrupted_runner_claim_never_resubmits_one_time_code(tmp_path):
    payload = {"id": "c" * 64, "action": "code", "code": "private-callback#state"}
    auth._write_private(tmp_path / "command.json", payload)
    auth._write_private(tmp_path / "claimed.json", {"id": payload["id"]})

    class NoReplayFlow:
        def submit_code(self, code):
            pytest.fail("Claimed code must not be replayed after runner failure")

    with pytest.raises(ValueError, match="Interrupted"):
        auth._consume_command(NoReplayFlow(), tmp_path)


def test_flow_finishing_during_code_delivery_keeps_success(tmp_path, monkeypatch):
    auth._write_private(tmp_path / "job.json", {"provider": "claude", "personal_mode": False})
    auth._write_private(tmp_path / "command.json", {"id": "d" * 64, "action": "code", "code": "late#code"})

    class RacingFlow:
        def __init__(self, *args, **kwargs):
            self.done = False

        def start(self):
            pass

        def snapshot(self):
            return {"status": "done" if self.done else "waiting"}

        def submit_code(self, code):
            self.done = True
            raise ValueError("Login no longer waiting")

        def take_credentials(self):
            return {"oauth_token": "success-token"}

        def cancel(self):
            pass

    monkeypatch.setattr(auth, "LoginFlow", RacingFlow)
    monkeypatch.setattr(auth, "local_proxy", lambda socket: nullcontext("http://127.0.0.1:9999"))
    auth.file_runner(tmp_path)
    assert json.loads((tmp_path / "state.json").read_text()) == {"status": "done"}
    assert json.loads((tmp_path / "result.json").read_text()) == {"oauth_token": "success-token"}
    assert json.loads((tmp_path / "ack.json").read_text()) == {"id": "d" * 64}


def test_worker_closes_only_owned_docker_client_even_on_early_return(controller, setup_project, monkeypatch):
    login(setup_project, expires=now() - 1)
    closed = []
    controller.close = lambda: closed.append(True)
    monkeypatch.setattr(auth.docker, "from_env", lambda: controller)
    auth.advance_logins()
    assert closed == [True]
    login(setup_project)
    auth.advance_logins(controller)
    assert closed == [True]
