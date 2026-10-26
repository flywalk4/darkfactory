"""Minimal onboarding creates a usable, isolated project team and shared routing."""

import json

from conftest import make_task

from darkfactory import agents, db, service


def create(client, name="Project", **extra):
    response = client.post(
        "/api/projects",
        json={"name": name, "remote": "https://example.invalid/project.git", "initialize": False, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def team(client, project):
    response = client.get(f"/api/projects/{project['id']}/agents")
    assert response.status_code == 200, response.text
    return response.json()


def test_minimal_project_creates_team_skills_and_common_pool(client):
    first = create(client)
    second = create(client, "Second")
    pools = client.get("/api/pools").json()
    assert len(pools) == 1
    assert pools[0]["name"] == "Общий пул"
    assert first["writer_pool"] == first["reviewer_pool"] == pools[0]["id"]
    assert second["writer_pool"] == second["reviewer_pool"] == pools[0]["id"]
    settings = team(client, first)
    assert {p["name"] for p in settings["profiles"]} == {
        "manager",
        "developer",
        "reviewer",
        "director",
    }
    assert settings["assignments"] == {
        "planner": "manager",
        "writer": "developer",
        "reviewer": "reviewer",
        "director": "director",
    }
    providers = {p["name"]: p["provider"] for p in settings["profiles"]}
    assert set(providers.values()) == {"project"}
    assert settings["skills"]
    assert settings["mcp_servers"] == []  # External capabilities are connected separately.
    assert first["checks"] == []  # Onboarding must not grant unchecked automatic merges.


def test_three_field_form_payload_queues_initialization_and_hides_token(client):
    response = client.post(
        "/api/projects",
        json={"name": "Private", "remote": "https://example.invalid/private.git", "git_token": "test-secret"},
    )
    assert response.status_code == 201, response.text
    assert "test-secret" not in response.text
    project = response.json()
    tasks = client.get(f"/api/projects/{project['id']}/tasks").json()
    assert sorted(
        key for t in tasks for key in t["role_config"] if key in {"initialization", "environment"}
    ) == [
        "environment",
        "initialization",
    ]
    assert len(team(client, project)["profiles"]) == 4
    with db.Session() as session:
        config = json.loads(db.decrypt(session.get(db.Project, project["id"]).secret))
        assert config["git_token"] == "test-secret"


def test_existing_pool_is_reused_and_team_changes_stay_project_scoped(client):
    pool = client.post("/api/pools", json={"name": "Subscriptions"}).json()
    first, second = create(client), create(client, "Second")
    assert first["writer_pool"] == second["writer_pool"] == pool["id"]
    assert len(client.get("/api/pools").json()) == 1
    settings = team(client, first)
    settings["profiles"][0]["instructions"] = "Custom manager instructions"
    for server in settings["mcp_servers"]:
        server.pop("configured_env")
        server.pop("configured_headers")
    assert client.put(f"/api/projects/{first['id']}/agents", json=settings).status_code == 200
    assert team(client, second)["profiles"][0]["instructions"] != "Custom manager instructions"
    response = client.put(f"/api/projects/{first['id']}", json={"name": "Renamed", "remote": first["remote"]})
    assert response.status_code == 200, response.text
    assert team(client, first)["profiles"][0]["instructions"] == "Custom manager instructions"
    assert response.json()["writer_pool"] == pool["id"]


def test_common_pool_remains_stable_after_adding_and_editing_pools(client):
    first = create(client)
    pool_id = first["writer_pool"]
    client.post("/api/pools", json={"name": "Dedicated"}).raise_for_status()
    client.put(f"/api/pools/{pool_id}", json={"name": "Company subscriptions"}).raise_for_status()
    second = create(client, "Second")
    assert second["writer_pool"] == second["reviewer_pool"] == pool_id


def test_explicit_routing_is_preserved(client):
    pool = client.post("/api/pools", json={"name": "Dedicated"}).json()
    project = create(client, writer_pool=pool["id"], reviewer_pool=None)
    assert project["writer_pool"] == pool["id"]
    assert project["reviewer_pool"] is None


def test_default_roles_match_explicit_providers_and_api_has_no_mcp(client):
    project = create(client, writer="api", reviewer="codex")
    settings = team(client, project)
    for profile in settings["profiles"]:
        assert profile["provider"] == "project"
        assert profile["mcp_servers"] == []
    task = make_task(client, project)
    with db.transaction() as session:
        row, task_row = session.get(db.Project, project["id"]), session.get(db.Task, task["id"])
        task_row.phase = "implement"
        assert service._routing(task_row, row) == ("api", True)
        task_row.phase = "review"
        assert service._routing(task_row, row) == ("codex", False)
        row.reviewer = "claude"
        assert service._routing(task_row, row) == ("claude", False)
        assert json.loads(db.decrypt(row.secret))["agents"]["profiles"]
    # Each call must return independent definitions, including nested skill and MCP lists.
    first, second = agents.default_settings(), agents.default_settings()
    first["profiles"][0]["skills"].clear()
    assert second["profiles"][0]["skills"]
