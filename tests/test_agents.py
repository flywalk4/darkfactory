import json
import tomllib

import pytest
from conftest import connect, make_task

from darkfactory import agents, db, service


def settings():
    return {
        "profiles": [
            {
                "name": "analyst",
                "provider": "codex",
                "instructions": "Write accepted analysis",
                "skills": ["spec"],
            },
            {"name": "developer", "provider": "claude", "mcp_servers": ["docs"]},
        ],
        "skills": [{"name": "spec", "description": "Clarify acceptance", "content": "Inspect edge cases."}],
        "mcp_servers": [
            {
                "name": "docs",
                "url": "https://docs.example.invalid/mcp",
                "enabled_tools": ["search"],
                "headers": {"Authorization": "Bearer project-secret"},
            }
        ],
        "assignments": {"planner": "analyst", "writer": "developer"},
    }


def test_project_profiles_preserve_secrets_and_apply_role_and_skill(client, setup_project, tmp_path):
    project, _ = setup_project
    path = f"/api/projects/{project['id']}/agents"
    response = client.put(path, json=settings())
    assert response.status_code == 200, response.text
    assert "project-secret" not in response.text
    public = response.json()
    assert public["mcp_servers"][0]["configured_headers"] == ["Authorization"]
    for server in public["mcp_servers"]:
        server.pop("configured_headers")
        server.pop("configured_env")
    assert client.put(path, json=public).status_code == 200
    with db.Session() as session:
        row = session.get(db.Project, project["id"])
        assert "project-secret" not in row.secret
        config = json.loads(db.decrypt(row.secret))
    profile = agents.resolve(config, "plan")
    assert profile["name"] == "analyst"
    # The prompt names the skill; its text lives only in the skill file.
    assert "Inspect edge cases." not in agents.skill_prompt(profile)
    assert "~/.agents/skills/spec/SKILL.md" in agents.skill_prompt(profile)
    # A read-only profile assigned as writer must still produce the task's files.
    assert agents.WRITER_NOTE not in agents.skill_prompt(profile, "plan")
    assert agents.WRITER_NOTE in agents.skill_prompt(profile, "implement")
    writer_prompt = agents.skill_prompt(agents.resolve(config, "implement"))
    assert "- docs (read-only): search" in writer_prompt and "project-secret" not in writer_prompt
    assert "MCP servers" not in agents.skill_prompt(profile)
    agents.materialize_skills(profile, tmp_path)
    assert (tmp_path / ".agents/skills/spec/SKILL.md").read_text() == "Inspect edge cases."
    writer = agents.resolve(config, "implement")
    assert writer["mcp_definitions"][0]["headers"]["Authorization"] == "Bearer project-secret"
    override = agents.resolve(config, "implement", {"writer": "analyst"})
    assert override["name"] == "analyst"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["assignments"].update(writer="missing"),
        lambda s: s["profiles"][0].update(skills=["missing"]),
        lambda s: s["mcp_servers"][0].update(name="../../escape"),
        lambda s: s["mcp_servers"][0].update(url="http://docs.example/mcp"),
        lambda s: s["mcp_servers"][0].update(enabled_tools=["*"]),
        lambda s: s["mcp_servers"][0].update(headers={"Authorization": "x\r\nInjected: x"}),
        lambda s: s["mcp_servers"][0].update(hosts=["jira.example.com"]),
        lambda s: s["mcp_servers"][0].update(
            transport="stdio",
            url=None,
            headers=None,
            command="mcp-atlassian",
            hosts=["https://jira.example.com"],
        ),
        lambda s: (
            s["assignments"].update(reviewer="developer") or s["mcp_servers"][0].update(read_only=False)
        ),
    ],
)
def test_invalid_profiles_fail_closed(client, setup_project, mutation):
    project, _ = setup_project
    payload = settings()
    mutation(payload)
    response = client.put(f"/api/projects/{project['id']}/agents", json=payload)
    assert response.status_code == 422


def test_task_role_override_and_missing_profile_are_validated(client, setup_project):
    project, _ = setup_project
    assert client.put(f"/api/projects/{project['id']}/agents", json=settings()).status_code == 200
    task = make_task(client, project, agent_profiles={"writer": "analyst"})
    assert task["role_config"]["profiles"] == {"writer": "analyst"}
    response = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={"title": "x", "request": "x", "agent_profiles": {"writer": "unknown"}},
    )
    assert response.status_code == 400


def test_profile_change_revokes_acceptance_and_keeps_budget(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.phase, row.state = "review", "needs_human"
        row.candidate_sha = row.verified_sha = row.reviewed_sha = "a" * 40
        row.active_seconds, row.corrections = 123, 2
    response = client.put(f"/api/projects/{project['id']}/agents", json=settings())
    assert response.status_code == 200
    current = client.get(f"/api/tasks/{task['id']}").json()
    assert current["verified_sha"] is None and current["reviewed_sha"] is None
    assert current["phase"] == "plan" and current["active_seconds"] == 123 and current["corrections"] == 2


def test_mcp_native_config_is_valid_toml_without_shell_interpolation():
    server = agents.MCPServer.model_validate(settings()["mcp_servers"][0]).model_dump()
    args = agents.codex_mcp_config([server])
    config = tomllib.loads(args[1])
    assert config["mcp_servers"]["docs"]["enabled_tools"] == ["search"]
    assert config["mcp_servers"]["docs"]["http_headers"]["Authorization"] == "Bearer project-secret"


def test_profile_access_is_project_scoped_and_observer_cannot_mutate(client, setup_project):
    project, _ = setup_project
    owner_token = client.headers["Authorization"]
    second = client.post(
        "/api/projects", json={"name": "Other", "remote": "https://example.invalid/other.git"}
    ).json()
    token = client.post("/api/tokens", json={"role": "observer", "project_ids": [project["id"]]}).json()[
        "token"
    ]
    client.headers["Authorization"] = "Bearer " + token
    assert client.get(f"/api/projects/{project['id']}/agents").status_code == 200
    assert client.put(f"/api/projects/{project['id']}/agents", json=settings()).status_code == 403
    assert client.get(f"/api/projects/{second['id']}/agents").status_code in {403, 404}
    client.headers["Authorization"] = owner_token


def test_profile_provider_drives_connection_selection(client, setup_project):
    project, pool = setup_project
    client.put(f"/api/projects/{project['id']}/agents", json=settings()).raise_for_status()
    preferred = connect(client, pool, "claude", "developer")
    connect(client, pool, "codex", "analyst")
    task = make_task(client, project)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.phase = "implement"
        assert (
            service.choose_connection(session, row, session.get(db.Project, project["id"])).id
            == preferred["id"]
        )


def test_untouched_default_skills_and_profiles_are_upgraded_custom_ones_kept():
    import copy

    from darkfactory import catalog as shipped

    old_review = (
        "\nРаботай независимо от авторской сессии. Прочитай задачу, diff, изменённые контракты и результаты\n"
        "проверок. Для каждого замечания установи конкретный триггер, неверное поведение и последствия.\n"
        "Проверь авторизацию, изоляцию, денежные лимиты, отмену и восстановление, когда изменение их затрагивает.\n"
        "Не требуй косметических правок без влияния на читаемость или поведение. Указывай файл и минимальный\n"
        "диапазон строк. Отделяй проверенные дефекты от вопросов. Не меняй код и не выполняй Git-запись.\n"
        "Подтверди готовность только после проверки условий задачи; при дефекте верни воспроизводимое замечание.\n"
    )
    description = "Найти подтверждённые дефекты в результате задачи."
    legacy = f"---\nname: code-review\ndescription: {description}\n---\n\n{old_review.strip()}\n"
    settings = {
        "skills": [
            {"name": "code-review", "description": description, "content": legacy},
            {"name": "safe-change", "description": "mine", "content": "Owner's own rules."},
        ],
        "profiles": [
            {
                "name": "reviewer",
                "instructions": "Проверь задачу, diff и тесты в независимой сессии. Верни только подтверждённые "
                "дефекты с конкретными строками и последствиями. Не меняй файлы, не делегируй и не выполняй "
                "Git-запись.",
                "skills": ["code-review", "regression-tests", "code-navigation"],
                "provider": "claude",
            }
        ],
    }
    before = copy.deepcopy(settings)
    assert agents.upgrade_defaults(settings)
    review = next(item for item in shipped.catalog()["skills"] if item["id"] == "code-review")["definition"]
    assert settings["skills"][0]["content"] == review["content"] and "confidence" in review["content"]
    assert settings["skills"][1] == before["skills"][1]
    assert settings["profiles"][0]["skills"] == ["code-review", "code-navigation"]
    assert settings["profiles"][0]["provider"] == "claude"
    assert not agents.upgrade_defaults(settings)


def test_unchanged_default_profiles_are_not_rewritten_on_every_start():
    from darkfactory import catalog as shipped

    manager = next(item for item in shipped.catalog()["profiles"] if item["id"] == "manager")["definition"]
    settings = {"skills": [], "profiles": [{**manager, "provider": "project"}]}
    assert not agents.upgrade_defaults(settings)


def test_prompt_preview_shows_factory_rules_and_the_role(client, setup_project):
    project, _ = setup_project
    for stage, rule, role in (
        ("writer", "You are the writer of this task", "developer"),
        ("reviewer", "Start from the code", "reviewer"),
        ("planner", '"files":["path/it/changes.ext"]', "manager"),
        ("director", "Director of an autonomous software factory", "director"),
    ):
        preview = client.get(f"/api/projects/{project['id']}/agents/preview", params={"stage": stage}).json()
        assert preview["profile"] == role
        assert rule in preview["sections"][0]["text"]
        assert "Agent role: " + role in preview["sections"][1]["text"]
    assert (
        client.get(f"/api/projects/{project['id']}/agents/preview", params={"stage": "x"}).status_code == 422
    )


def test_planner_chooses_roles_by_use_when():
    from darkfactory import catalog as shipped

    manager = next(item for item in shipped.catalog()["profiles"] if item["id"] == "manager")["definition"]
    assert manager["use_when"]
    settings = agents.default_settings()
    assert all(profile["use_when"] for profile in settings["profiles"])
    stale = {**settings, "profiles": [{**p, "use_when": ""} for p in settings["profiles"]]}
    assert agents.upgrade_defaults(stale)
    assert all(profile["use_when"] for profile in stale["profiles"])


def test_stdio_mcp_hosts_reach_task_egress(client, setup_project, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from darkfactory import execution

    project, _ = setup_project
    payload = settings()
    payload["mcp_servers"][0] = {
        "name": "docs",
        "transport": "stdio",
        "command": "mcp-atlassian",
        "hosts": ["jira.example.com", "jira.example.com:8443"],
        "enabled_tools": ["jira_get_issue"],
        "env": {"JIRA_PERSONAL_TOKEN": "project-secret"},
    }
    assert client.put(f"/api/projects/{project['id']}/agents", json=payload).status_code == 200
    task = make_task(client, project)
    seen = []

    class Captured(Exception):
        pass

    @contextmanager
    def proxy(_path, allowed, _denied, private):
        seen.append((allowed, private))
        yield

    def create(_image, **kwargs):
        raise Captured()

    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(execution, "egress_proxy", proxy)
    with db.Session() as session, pytest.raises(Captured):
        execution.DockerExecutor().run(
            session.get(db.Task, task["id"]),
            session.get(db.Project, project["id"]),
            None,
            "implement",
            lambda: True,
            60,
        )
    stored = client.get(f"/api/projects/{project['id']}/agents").json()
    for server in stored["mcp_servers"]:
        server.pop("configured_env"), server.pop("configured_headers"), server.pop("hosts")
    assert client.put(f"/api/projects/{project['id']}/agents", json=stored).status_code == 200
    assert client.get(f"/api/projects/{project['id']}/agents").json()["mcp_servers"][0]["hosts"] == [
        "jira.example.com",
        "jira.example.com:8443",
    ]
    allowed, private = seen[0]
    assert {("jira.example.com", 443), ("jira.example.com", 8443)} <= allowed & private


def test_static_prompt_text_stays_short():
    # Before the trim: WRITER_NOTE was 306 chars, the MCP header 150 chars; sent on every agent turn.
    assert len(agents.WRITER_NOTE) <= 230
    profile = {"name": "a", "instructions": "", "skill_definitions": [], "mcp_definitions": []}
    profile["mcp_definitions"] = [{"name": "docs", "read_only": True, "enabled_tools": ["x"]}]
    header = agents.mcp_prompt(profile).split("\n")[1]
    assert len(header) <= 100 and "codex mcp list" in header
