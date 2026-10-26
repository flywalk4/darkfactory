import json

from conftest import connect, make_task

from darkfactory import db


def test_organization_isolation_and_token_ownership(client, setup_project):
    project, pool = setup_project
    connection = connect(client, pool)
    task = make_task(client, project)
    other = client.post("/api/organizations", json={"name": "Second company"}).json()
    client.headers["Authorization"] = "Bearer " + other["token"]
    assert client.get("/api/projects").json() == []
    assert client.get("/api/tasks/" + task["id"]).status_code == 404
    assert client.get("/api/tasks/" + task["id"] + "/artifacts/diff.patch").status_code == 404
    assert (
        client.post(
            "/api/connections/" + connection["id"] + "/reconnect", json={"credentials": {"oauth_token": "x"}}
        ).status_code
        == 404
    )
    assert len(client.get("/api/tokens").json()) == 1
    assert client.get("/api/me").json()["organization"]["name"] == "Second company"


def test_credentials_cannot_multiply_account_slots(client, setup_project):
    _, pool = setup_project
    connect(client, pool, account="original")
    response = client.post(
        "/api/connections",
        json={
            "name": "Copy",
            "provider": "claude",
            "account_key": "new-label",
            "pool_ids": [pool["id"]],
            "credentials": {"oauth_token": "not-a-real-token-original"},
        },
    )
    assert response.status_code == 409
    assert len(client.get("/api/connections").json()) == 1


def test_local_repository_cannot_bypass_organization_isolation(client):
    original = client.headers["Authorization"]
    assert (
        client.post(
            "/api/projects", json={"name": "Private", "remote": "/repos/company/repo.git"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/projects", json={"name": "Same company", "remote": "/repos/company/repo.git"}
        ).status_code
        == 201
    )
    other = client.post("/api/organizations", json={"name": "Separate company"}).json()
    client.headers["Authorization"] = "Bearer " + other["token"]
    for remote in (
        "/repos/company/repo.git",
        "/repos/company/./repo.git",
        "/repos/company/repo.git/.git",
        "/repos/company",
    ):
        assert (
            client.post(
                "/api/projects", json={"name": "Cross-organization clone", "remote": remote}
            ).status_code
            == 403
        )
    assert (
        client.post(
            "/api/projects", json={"name": "Own repository", "remote": "/repos/another/repo.git"}
        ).status_code
        == 201
    )
    client.headers["Authorization"] = original


def test_project_secrets_private_and_preserved(client, setup_project):
    project, _ = setup_project
    updated = client.put(
        "/api/projects/" + project["id"],
        json={
            "name": project["name"],
            "remote": project["remote"],
            "test_env": {"TEST_PASSWORD": "private-test-password"},
            "executor_image": "mycompany/test-runner:1",
            "allowed_hosts": ["test.internal:443"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert "private-test-password" not in updated.text
    assert updated.json()["test_env_keys"] == ["TEST_PASSWORD"]
    client.put("/api/projects/" + project["id"], json={"name": "Renamed", "remote": project["remote"]})
    with db.Session() as session:
        saved = session.get(db.Project, project["id"])
        secret = json.loads(db.decrypt(saved.secret))
        assert secret["test_env"]["TEST_PASSWORD"] == "private-test-password"
        assert secret["executor_image"] == "mycompany/test-runner:1"
    invalid = client.put(
        "/api/projects/" + project["id"],
        json={"name": "Invalid", "remote": project["remote"], "test_env": {"HOME": "/workspace"}},
    )
    assert invalid.status_code == 400


def test_dependency_changes_invalidate_acceptance_and_release_integration(client, setup_project):
    project, _ = setup_project
    task, dependency = make_task(client, project), make_task(client, project)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.phase, row.verified_sha, row.reviewed_sha = "merge", "old", "old"
        session.get(db.Project, project["id"]).integration_task = row.id
    assert (
        client.post(
            "/api/tasks/" + task["id"] + "/links", json={"kind": "depends_on", "other_id": dependency["id"]}
        ).status_code
        == 201
    )
    detail = client.get("/api/tasks/" + task["id"]).json()
    assert (
        detail["phase"] == "implement" and detail["verified_sha"] is None and detail["reviewed_sha"] is None
    )
    assert client.delete("/api/tasks/" + task["id"] + "/links/" + detail["links"][0]["id"]).status_code == 200
    assert client.get("/api/tasks/" + task["id"]).json()["links"] == []
    with db.Session() as session:
        assert session.get(db.Project, project["id"]).integration_task is None


def test_project_destinations_refuse_private_and_single_label_targets(client, setup_project):
    project, _ = setup_project
    url = "/api/projects/" + project["id"]

    def put(**values):
        return client.put(url, json={"name": project["name"], "remote": project["remote"], **values})

    assert put(allowed_hosts=["db:5432"]).status_code == 400
    assert put(allowed_hosts=["10.0.0.5:443", "feeds.corp.example:443"]).status_code == 200
    for remote in (
        "http://127.0.0.1/repo.git",
        "https://169.254.169.254/latest",
        "ssh://git@100.64.0.1/repo.git",
        "git@localhost:repo.git",
        "https://gitlab/repo.git",
    ):
        assert put(remote=remote).status_code == 400, remote
    assert put(forge="gitlab", forge_url="http://172.18.0.2", forge_repo="a/b").status_code == 400
    # The stored allowed_hosts lists 10.0.0.5:443 as an IP literal, so that exact private target passes.
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = "https://10.0.0.5/repo.git"
    assert (
        client.put(url, json={"name": project["name"], "remote": "https://10.0.0.5/repo.git"}).status_code
        == 200
    )


def test_test_env_accepts_only_test_app_e2e_names(client, setup_project):
    project, _ = setup_project
    url = "/api/projects/" + project["id"]
    base = {"name": project["name"], "remote": project["remote"]}
    assert (
        client.put(
            url, json={**base, "test_env": {"TEST_TOKEN": "a", "APP_URL": "b", "E2E_USER": "c"}}
        ).status_code
        == 200
    )
    for name in ("DATABASE_URL", "PATH", "NODE_OPTIONS", "TEST_", "test_lower"):
        assert client.put(url, json={**base, "test_env": {name: "x"}}).status_code == 400, name
    # Values saved earlier stay readable even when a renamed rule would now refuse them.
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.secret = db.encrypt(
            json.dumps({**json.loads(db.decrypt(row.secret)), "test_env": {"LEGACY": "1"}})
        )
    assert client.put(url, json={**base, "name": "Renamed"}).status_code == 200
    assert client.get("/api/projects").json()[0]["test_env_keys"] == ["LEGACY"]
