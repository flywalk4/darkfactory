import json

from conftest import connect
from test_standqa import PNG, configure

from darkfactory import db, schedules, standbuild


class Builder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(
        self, task, project, connection, role, heartbeat, timeout, *, prompt_override=None, output_schema=None
    ):
        self.calls.append((role, prompt_override, output_schema))
        run = db.ROOT / "artifacts" / task.id / "browser" / f"gen{len(self.calls)}"
        run.mkdir(parents=True, exist_ok=True)
        (run / "execution.json").write_text("{}")
        (run / "form.png").write_bytes(PNG)
        return {"status": "ok", "text": json.dumps(self.result)}


def result(built=True, problems=(), scores=(4, 3, 3, 2)):
    return {
        "summary": "Собрал заявки на отпуск",
        "built": built,
        "minutes": 12.5,
        "compile_errors": 1,
        "test_failures": 0,
        "scores": dict(zip(("completeness", "correctness", "usability", "visual"), scores, strict=True)),
        "problems": list(problems),
    }


def problem(title, category="components"):
    return {
        "title": title,
        "category": category,
        "severity": "high",
        "steps": "1. Открыть форму заявки",
        "expected": "Поле даты",
        "actual": "Текстовое поле",
        "area": "lib/components/date",
        "screenshots": ["form.png"],
    }


def build_schedule(client, project):
    base = f"/api/projects/{project['id']}/schedules"
    return client.post(base, json={"kind": "stand_build", "cron": "0 4 * * *"}).json()


def test_build_rotates_requests_scores_and_files_platform_problems(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    configure(client, project, build_tasks=["Заявки на отпуск", "Каталог товаров"])
    schedule = build_schedule(client, project)
    assert not standbuild.run(Builder(result()))  # Not requested yet.
    assert client.post(f"/api/schedules/{schedule['id']}/run").json()["queued"]

    first = Builder(result(problems=[problem("Нет поля даты в форме"), problem("Нет поля даты в форме!")]))
    assert standbuild.run(first)
    role, prompt, schema = first.calls[0]
    assert role == "qa" and schema == standbuild.SCHEMA and "Заявки на отпуск" in prompt
    with db.Session() as session:
        tasks = session.query(db.Task).filter_by(project_id=project["id"]).all()
    assert [task.title for task in tasks] == ["Нет поля даты в форме"]
    assert tasks[0].role_config["qa"]["build"] == "components"
    assert "Категория причины: components" in tasks[0].request

    client.post(f"/api/schedules/{schedule['id']}/run")
    second = Builder(result(built=False, scores=(0, 0, 0, 0)))
    assert standbuild.run(second)
    assert "Каталог товаров" in second.calls[0][1] and "Нет поля даты в форме" in second.calls[0][1]
    job = client.get(f"/api/projects/{project['id']}/schedules").json()["jobs"]["stand_build"]
    assert job["last"]["built"] is False and job["last"]["task"] == "Каталог товаров"
    assert job["average"] == {
        "runs": 2,
        "built": 0.5,
        "scores": {"completeness": 2.0, "correctness": 1.5, "usability": 1.5, "visual": 1.0},
    }
    # The rotation comes back to the first request.
    client.post(f"/api/schedules/{schedule['id']}/run")
    third = Builder(result())
    assert standbuild.run(third)
    assert "Заявки на отпуск" in third.calls[0][1] and "built=True" in third.calls[0][1]


def test_build_without_a_stand_is_refused(client, setup_project):
    project, _ = setup_project
    schedule = build_schedule(client, project)
    assert (
        client.post(f"/api/schedules/{schedule['id']}/run").json()["last_result"] == "Адрес стенда не задан"
    )
    assert schedules.QUEUED["stand_build"]
