import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from conftest import connect, make_task
from sqlalchemy import select
from test_pipeline import drive, git, seed

from darkfactory import agents, db, execution, lessons, schedules
from darkfactory.worker import Worker, accept_merge


def cases(project_id):
    with db.Session() as session:
        return (session.get(db.Project, project_id).health or {}).get("lessons") or []


def merge(task_id, *, corrections=0, events=(), qa=False):
    with db.transaction() as session:
        task = session.get(db.Task, task_id)
        task.corrections = corrections
        if qa:
            task.role_config = {**(task.role_config or {}), "qa": True}
        for kind in events:
            db.event(session, task, kind, "x")
        accept_merge(session, task)


def lessons_schedule(project_id):
    with db.Session() as session:
        return session.scalar(
            select(schedules.Schedule.id).where(
                schedules.Schedule.project_id == project_id, schedules.Schedule.kind == "lessons"
            )
        )


def fire(schedule_id):
    with db.transaction() as session:
        schedule = session.get(schedules.Schedule, schedule_id)
        result = schedules.fire(session, schedule)
        return result, schedule.last_task_id


def test_only_tasks_with_detours_feed_the_lessons(client, setup_project):
    project, _ = setup_project
    plain = make_task(client, project, title="Plain")
    merge(plain["id"])
    assert cases(project["id"]) == []

    corrected = make_task(client, project, title="Corrected")
    artifacts = db.ROOT / "artifacts" / corrected["id"]
    artifacts.mkdir(parents=True, exist_ok=True)
    token = "ghp_" + "a" * 36
    findings = [{"text": "Missing   test for the empty list"}, {"text": f"Second {token}"}, {"text": "Third"}]
    (artifacts / "review.json").write_text(json.dumps({"approved": False, "findings": findings}))
    merge(corrected["id"], corrections=2)
    repeated = make_task(client, project, title="Repeated")
    merge(repeated["id"], events=["review_repeated"])
    stopped = make_task(client, project, title="Stopped")
    merge(stopped["id"], events=["supervisor"])
    tester = make_task(client, project, title="Stand bug")
    merge(tester["id"], qa=True)

    recorded = cases(project["id"])
    assert [entry["task_id"] for entry in recorded] == [
        corrected["id"],
        repeated["id"],
        stopped["id"],
        tester["id"],
    ]
    assert recorded[0]["notes"] == ["Missing test for the empty list", "Second <secret>"]
    assert recorded[0]["reasons"] == ["возвратов ревью: 2"]

    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.health = {**row.health, "lessons": [recorded[0]] * lessons.RECORDS}
    merge(make_task(client, project, title="One more")["id"], corrections=1)
    assert len(cases(project["id"])) == lessons.RECORDS
    assert cases(project["id"])[-1]["title"] == "One more"


def test_schedule_creates_one_task_only_when_cases_wait(client, setup_project):
    project, _ = setup_project
    schedule = lessons_schedule(project["id"])
    assert schedule, "new projects get the lessons schedule by default"
    result, created = fire(schedule)
    assert result == "Новых уроков нет" and created is None
    with db.Session() as session:
        assert not [task for task in session.scalars(select(db.Task)) if lessons.active(task)]

    merge(make_task(client, project, title="Corrected")["id"], corrections=1)
    result, created = fire(schedule)
    assert created and result == f"Создана задача {created}"
    assert cases(project["id"]) == []
    with db.Session() as session:
        task = session.get(db.Task, created)
        assert task.title == lessons.TITLE and lessons.active(task)
        assert "«Corrected»" in task.request and lessons.PATH in task.request

    merge(make_task(client, project, title="Again")["id"], corrections=1)
    result, again = fire(schedule)
    assert again == created and result.startswith("Пропущено")
    assert len(cases(project["id"])) == 1


def test_existing_projects_get_the_lessons_schedule_once(client, setup_project):
    project, _ = setup_project
    with db.transaction() as session:
        session.delete(session.get(schedules.Schedule, lessons_schedule(project["id"])))
        row = session.get(db.Project, project["id"])
        row.health = {key: value for key, value in row.health.items() if key != "lessons_seeded"}
    assert schedules.seed_all() == 1
    assert schedules.seed_all() == 0
    with db.Session() as session:
        assert session.get(schedules.Schedule, lessons_schedule(project["id"])).cron == schedules.LESSONS_CRON


class LessonsExecutor:
    def __init__(self, invalid_once=False):
        self.invalid_once = invalid_once
        self.attempts = 0

    def stop(self, *_):
        pass

    def run(self, task, project, connection, role, heartbeat, timeout):
        work = db.ROOT / "workspaces" / task.id
        if role == "plan":
            return {"status": "ok", "plan": {"plan": "Rewrite the rules", "questions": []}}
        if role == "implement":
            self.attempts += 1
            (work / lessons.DIRECTORY).mkdir(parents=True, exist_ok=True)
            (work / lessons.PATH).write_text("- Делай тест на пустой список — ревью возвращало без него.\n")
            (work / "value.py").write_text("VALUE = 42\n" if self.invalid_once else "VALUE = 0\n")
            self.invalid_once = False
            return {"status": "ok", "text": "Rules updated"}
        if role == "review":
            return {"status": "ok", "review": {"approved": True, "findings": []}}
        return {"status": "ok", "text": "Repository checks passed"}


def test_lessons_task_that_touches_another_file_does_not_merge(client, setup_project, tmp_path):
    project, pool = setup_project
    connect(client, pool, "codex", "writer")
    connect(client, pool, "claude", "reviewer")
    remote = seed(tmp_path)
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        row.remote = str(remote)
        row.checks = []
    merge(make_task(client, project, title="Corrected")["id"], corrections=1)
    _, task_id = fire(lessons_schedule(project["id"]))
    executor = LessonsExecutor(invalid_once=True)
    result = drive(Worker(executor), task_id)
    # The writer's first try changed value.py: it went back for a correction instead of merging.
    assert result.state == "merged", result.reason
    assert executor.attempts == 2 and result.corrections == 1
    assert git(remote, "diff", "--name-only", "main", "preprod").splitlines() == [lessons.PATH]
    # The lessons task itself is not a new lesson.
    assert cases(project["id"]) == []


def test_merge_guard_and_writer_check_reject_other_files(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    git(work, "init", "-b", "main")
    (work / "value.py").write_text("VALUE = 0\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "seed")
    base = git(work, "rev-parse", "HEAD")
    repo = SimpleNamespace(work=work, git=lambda *args, **kwargs: git(work, *args))
    (work / lessons.DIRECTORY).mkdir(parents=True)
    (work / lessons.PATH).write_text("- Делай X — потому что Y.\n")
    lessons.validate(repo)
    (work / "value.py").write_text("VALUE = 42\n")
    with pytest.raises(ValueError):
        lessons.validate(repo)
    git(work, "add", ".")
    git(work, "commit", "-m", "both")
    with pytest.raises(ValueError):
        lessons.only_lessons(repo, base, git(work, "rev-parse", "HEAD"))
    (work / "value.py").write_text("VALUE = 0\n")
    git(work, "commit", "-am", "restore")
    lessons.only_lessons(repo, base, git(work, "rev-parse", "HEAD"))
    (work / lessons.PATH).write_text("- rule\n" * (lessons.LINES + 1))
    with pytest.raises(ValueError):
        lessons.validate(repo)


def test_writer_prompt_names_the_skill_and_not_the_rules(client, setup_project, monkeypatch, tmp_path):
    project, _ = setup_project
    task = make_task(client, project)
    rule = "Не трогай миграции руками — их генерирует alembic."
    work = db.ROOT / "workspaces" / task["id"]
    (work / lessons.DIRECTORY).mkdir(parents=True, exist_ok=True)
    (work / lessons.PATH).write_text(rule + "\n")

    class Captured(Exception):
        pass

    jobs = []

    def create(_image, **kwargs):
        # The job file exists only while the run is being started.
        jobs.extend(json.loads(path.read_text()) for path in (db.ROOT / "jobs").glob("*/input.json"))
        raise Captured()

    @contextmanager
    def proxy(*_args):
        yield

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
    (job,) = jobs
    line = f"- project-lessons: {lessons.DESCRIPTION} (~/.agents/skills/project-lessons/SKILL.md)"
    assert line in job["prompt"]
    assert rule not in job["prompt"]
    agents.materialize_skills(job["agent_profile"], tmp_path)
    assert rule in (tmp_path / ".agents/skills/project-lessons/SKILL.md").read_text()
