"""Guards that stop cycles which cannot converge: scratch commits and repeated review findings."""

import json

from conftest import connect, make_task
from sqlalchemy import select
from test_pipeline import DeterministicExecutor, drive, git, seed

from darkfactory import db
from darkfactory.worker import Worker, repeated_review


def prepared(client, setup_project, tmp_path):
    project, pool = setup_project
    remote = seed(tmp_path)
    connect(client, pool)
    with db.transaction() as session:
        session.get(db.Project, project["id"]).remote = str(remote)
    return remote, make_task(client, project)


def events(task_id):
    with db.Session() as session:
        return list(
            session.scalars(select(db.Event).where(db.Event.task_id == task_id).order_by(db.Event.id))
        )


class ScratchOnce(DeterministicExecutor):
    def __init__(self):
        super().__init__()
        self.scratch = True

    def run(self, task, project, connection, role, heartbeat, timeout):
        work = db.ROOT / "workspaces" / task.id
        if role == "implement" and self.scratch:
            self.scratch = False
            for index in range(3):
                path = work / ".repro.aB3dE9" / f"node_modules_copy/{index}.js"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("junk\n")
            (work / "value.py").write_text("VALUE = 42\n")
            return {"status": "ok", "text": "Fixed with experiments"}
        if role == "implement":
            import shutil

            shutil.rmtree(work / ".repro.aB3dE9", ignore_errors=True)
        return super().run(task, project, connection, role, heartbeat, timeout)


def test_scratch_directory_is_removed_by_the_factory(client, setup_project, tmp_path):
    remote, task = prepared(client, setup_project, tmp_path)
    result = drive(Worker(ScratchOnce()), task["id"])
    assert result.state == "merged", result.reason
    log = events(task["id"])
    assert not [e for e in log if e.kind == "correction"]
    assert [e for e in log if e.kind == "tidied" and ".repro.aB3dE9/" in e.message]
    assert ".repro" not in git(remote, "ls-tree", "-r", "--name-only", "preprod")
    assert "value.py" in git(remote, "ls-tree", "-r", "--name-only", "preprod")
    kept = db.ROOT / "artifacts" / task["id"] / "run-output" / ".repro.aB3dE9" / "node_modules_copy" / "0.js"
    assert kept.read_text() == "junk\n"


def test_too_many_new_files_asks_to_clean_up(client, setup_project, tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFACTORY_MAX_NEW_FILES", "2")

    class Generated(DeterministicExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            work = db.ROOT / "workspaces" / task.id
            if role == "implement" and not (work / "done").exists():
                (work / "done").write_text("")
                for index in range(3):
                    (work / f"dump{index}.json").write_text("{}")
            elif role == "implement":
                for index in range(3):
                    (work / f"dump{index}.json").unlink(missing_ok=True)
                (work / "done").unlink()
            return super().run(task, project, connection, role, heartbeat, timeout)

    _, task = prepared(client, setup_project, tmp_path)
    assert drive(Worker(Generated()), task["id"]).state == "merged"
    corrections = [e.message for e in events(task["id"]) if e.kind == "correction"]
    assert len(corrections) == 1 and "новых файлов" in corrections[0]


class Rejects(DeterministicExecutor):
    def __init__(self, *findings):
        super().__init__()
        self.findings = list(findings)

    def run(self, task, project, connection, role, heartbeat, timeout):
        if role != "review":
            return super().run(task, project, connection, role, heartbeat, timeout)
        finding = self.findings.pop(0) if len(self.findings) > 1 else self.findings[0]
        review = {"approved": False, "findings": [finding]}
        return {"status": "ok", "text": json.dumps(review, ensure_ascii=False), "review": review}


def test_same_review_finding_twice_stops_the_loop(client, setup_project, tmp_path):
    _, task = prepared(client, setup_project, tmp_path)
    finding = "[P1] Отчёт docs/reviews/analysis.md отсутствует; diff пуст, требуемый результат не сохранён."
    result = drive(Worker(Rejects(finding, finding.replace("P1", "P2") + " Повторно.")), task["id"])
    assert (result.state, result.reason, result.corrections) == ("needs_human", "review_repeated", 1)
    assert [e.kind for e in events(task["id"])].count("review_repeated") == 1


def test_different_findings_keep_correcting(client, setup_project, tmp_path):
    _, task = prepared(client, setup_project, tmp_path)
    executor = Rejects(
        "[P1] Не обработан пустой ответ сервера в компоненте сравнения веток.",
        "[P2] Регрессионный тест отсутствует для мобильной ширины экрана настроек.",
        "[P2] Логи содержат токен авторизации в заголовке запроса.",
        "[P3] Миграция базы данных необратима, откат невозможен.",
        "[P2] Документация описывает старый формат конфигурации проекта.",
        "[P1] Кэширование ответа игнорирует смену пользователя сессии.",
        "[P2] Обработчик таймаута глотает исключение без записи журнала.",
    )
    result = drive(Worker(executor), task["id"])
    assert result.reason == "correction_limit"


def test_similarity_reads_findings_not_json_noise():
    with db.transaction() as session:
        task = session.scalar(select(db.Task))
        assert task is None or not repeated_review(session, task, '{"approved":false,"findings":[]}')


def test_one_task_cannot_spend_the_tree_budget_on_corrections(client, setup_project, tmp_path, monkeypatch):
    _, task = prepared(client, setup_project, tmp_path)
    with db.transaction() as session:
        session.get(db.Project, session.get(db.Task, task["id"]).project_id).max_corrections = 20
    monkeypatch.setenv("DARKFACTORY_TASK_CORRECTIONS", "2")
    executor = Rejects(
        "[P1] Не обработан пустой ответ сервера в компоненте сравнения веток.",
        "[P2] Регрессионный тест отсутствует для мобильной ширины экрана настроек.",
        "[P2] Логи содержат токен авторизации в заголовке запроса.",
        "[P3] Миграция базы данных необратима, откат невозможен.",
    )
    result = drive(Worker(executor), task["id"])
    assert (result.state, result.reason, result.corrections) == ("needs_human", "correction_limit", 2)


def test_token_budget_warns_then_stops_and_resume_grants_more(client, setup_project, tmp_path, monkeypatch):
    _, task = prepared(client, setup_project, tmp_path)
    monkeypatch.setenv("DARKFACTORY_TASK_TOKENS", "1000")
    spent = iter([850, 850, 1200, 1200, 1200])
    monkeypatch.setattr("darkfactory.usage.spent_tokens", lambda _task_id: next(spent, 1200))
    executor = Rejects(
        "[P1] Не обработан пустой ответ сервера в компоненте сравнения веток.",
        "[P2] Регрессионный тест отсутствует для мобильной ширины экрана настроек.",
        "[P3] Миграция базы данных необратима, откат невозможен.",
    )
    result = drive(Worker(executor), task["id"])
    assert (result.state, result.reason) == ("needs_human", "token_limit")
    kinds = [e.kind for e in events(task["id"])]
    assert kinds.count("token_warning") == 1
    assert client.post(f"/api/tasks/{task['id']}/actions", json={"action": "resume"}).status_code == 200
    with db.Session() as session:
        row = session.get(db.Task, task["id"])
        assert row.state == "queued" and row.role_config["token_credit"] == 1200


def test_cancelled_members_do_not_spend_tree_corrections(client, setup_project):
    from darkfactory import service

    project, _ = setup_project
    root = make_task(client, project)
    child = make_task(client, project)
    with db.transaction() as session:
        session.add(db.Link(id=db.uid(), task_id=root["id"], other_id=child["id"], kind="parent"))
        row = session.get(db.Task, child["id"])
        row.corrections, row.state = 8, "cancelled"
    with db.Session() as session:
        assert service.execution_limits(session, session.get(db.Task, root["id"]))["corrections"] == 0


class LogsOnce(DeterministicExecutor):
    def __init__(self):
        super().__init__()
        self.logs = True

    def run(self, task, project, connection, role, heartbeat, timeout):
        work = db.ROOT / "workspaces" / task.id
        if role == "implement" and self.logs:
            self.logs = False
            path = work / "docs" / "reviews" / "validation" / "logs" / "e2e.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("passed\n")
            (work / "value.py").write_text("VALUE = 42\n")
            return {"status": "ok", "text": "Fixed, logs committed"}
        if role == "implement":
            import shutil

            shutil.rmtree(work / "docs" / "reviews" / "validation", ignore_errors=True)
        return super().run(task, project, connection, role, heartbeat, timeout)


def test_run_logs_are_removed_by_the_factory(client, setup_project, tmp_path):
    remote, task = prepared(client, setup_project, tmp_path)
    result = drive(Worker(LogsOnce()), task["id"])
    assert result.state == "merged", result.reason
    log = events(task["id"])
    assert not [e for e in log if e.kind == "correction"]
    assert [e for e in log if e.kind == "tidied" and "e2e.log" in e.message]
    tree = git(remote, "ls-tree", "-r", "--name-only", "preprod")
    assert "e2e.log" not in tree and "docs/reviews/validation" not in tree and "value.py" in tree


def test_reviewer_reads_implementer_evidence(tmp_path):
    from darkfactory import execution

    run = db.ROOT / "artifacts" / "t-proof" / "browser" / "g1"
    run.mkdir(parents=True)
    (run / "execution.json").write_text(json.dumps({"role": "implement"}))
    (run / "transcript.jsonl").write_text("")
    assert execution.implementation_evidence("t-proof") is None
    (run / "e2e.log").write_text("ok")
    assert execution.implementation_evidence("t-proof") == run


def test_run_output_pattern_spares_code_and_documentation_images():
    from darkfactory.gitops import EVIDENCE

    junk = [
        "docs/reviews/settings-branch/settings-loading-1440.png",
        "docs/reviews/settings-validation/archive-evidence.py",
        "docs/reviews/settings-validation/final/correction-1/checks.tsv",
        "docs/reviews/x/final/browser-evidence.json",
        "docs/reviews/x/current/logs/browser.log",
        "sources.sha256",
    ]
    kept = [
        "docs/reviews/settings-validation/README.md",
        "docs/images/logo.png",
        "src/validation/rules.ts",
        "src/app/evidence.service.ts",
        "src/app/logs/view.ts",
    ]
    assert all(EVIDENCE.search(name) for name in junk)
    assert not any(EVIDENCE.search(name) for name in kept)


def test_correction_prompt_lists_previous_changes(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from darkfactory import execution

    monkeypatch.setattr(execution, "ROOT", tmp_path)
    work = tmp_path / "workspaces" / "t1"
    work.mkdir(parents=True)

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=T", "-c", "user.email=t@localhost", *args], cwd=work, text=True
        ).strip()

    git("init", "-q")
    (work / "a.py").write_text("A = 1\n")
    git("add", ".")
    git("commit", "-qm", "seed")
    target = git("rev-parse", "HEAD")
    (work / "a.py").write_text("A = 2\n")
    (work / "b.py").write_text("B = 1\n")
    git("add", ".")
    git("commit", "-qm", "attempt")
    listed = execution.previous_changes(SimpleNamespace(id="t1", target_sha=target))
    assert "a.py" in listed and "b.py" in listed and "2 files changed" in listed
    assert execution.previous_changes(SimpleNamespace(id="missing", target_sha=target)) == ""


def test_later_findings_do_not_cost_another_cycle_or_fill_the_backlog(client, setup_project, tmp_path):
    from darkfactory.backlog import BacklogItem

    _, task = prepared(client, setup_project, tmp_path)
    executor = Rejects("[LATER] Вынести общий код сравнения в хелпер.")
    assert drive(Worker(executor), task["id"]).state == "merged"
    assert not [e for e in events(task["id"]) if e.kind == "correction"]
    with db.transaction() as session:
        items = list(session.scalars(select(BacklogItem).where(BacklogItem.project_id == task["project_id"])))
    # The note stays in the task's review; only the owner moves it to the backlog.
    assert items == []


def test_correction_handoff_carries_only_blocking_findings():
    from darkfactory.worker import without_later

    message = json.dumps(
        {"approved": False, "findings": ["[P1] Падает на пустом ответе.", "[LATER] Переименовать."]}
    )
    assert json.loads(without_later(message))["findings"] == ["[P1] Падает на пустом ответе."]


def test_writer_later_lines_stay_on_the_task(client, setup_project, tmp_path):
    from darkfactory.backlog import BacklogItem
    from darkfactory.worker import deferred_lines

    _, task = prepared(client, setup_project, tmp_path)

    class Defers(DeterministicExecutor):
        def run(self, task, project, connection, role, heartbeat, timeout):
            result = super().run(task, project, connection, role, heartbeat, timeout)
            if role == "implement":
                result["text"] = "Готово.\n[LATER] Добавить тур по Studio"
            return result

    assert drive(Worker(Defers()), task["id"]).state == "merged"
    with db.transaction() as session:
        items = list(session.scalars(select(BacklogItem).where(BacklogItem.project_id == task["project_id"])))
    assert items == []
    notes = [e for e in events(task["id"]) if e.kind == "later_notes"]
    assert [e.data["items"] for e in notes] == [["Добавить тур по Studio"]]

    text = "Готово.\n- [LATER] Добавить тур по Studio\n[LATER]   Покрыть раздел Pro\nОбычная строка [LATER] внутри"
    assert deferred_lines(text) == ["[LATER] Добавить тур по Studio", "[LATER]   Покрыть раздел Pro"]
    assert deferred_lines(None) == []


def test_re_review_is_given_the_earlier_findings(client, setup_project, tmp_path):
    from darkfactory.execution import earlier_findings

    _, task = prepared(client, setup_project, tmp_path)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        db.event(
            session,
            row,
            "correction",
            json.dumps({"approved": False, "findings": ["[P2] a.ts:1 — дубль", "[LATER] тур"]}),
        )
        db.event(session, row, "correction", "$ npm test\nexit=1")
    assert earlier_findings(task["id"]) == ["[P2] a.ts:1 — дубль"]


def test_resumed_session_gets_the_owner_answer(client, setup_project, tmp_path):
    from darkfactory.execution import continuation

    _, task = prepared(client, setup_project, tmp_path)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        db.event(session, row, "resume", "старый ответ")
        db.event(session, row, "phase_finished", "implement")
        db.event(session, row, "resume", "посмотри сам, пароль был такой")
        db.event(session, row, "resume", "resume")
    with db.transaction() as session:
        text = continuation(tmp_path / "missing-handoff", session.get(db.Task, task["id"]))
    assert "посмотри сам, пароль был такой" in text and "старый ответ" not in text


def test_writer_gets_and_keeps_project_notes(tmp_path):
    from darkfactory import execution

    assert "none yet" in execution.project_notes(tmp_path)
    (tmp_path / execution.NOTES).write_text("- Frontend tests: cd src/frontend && npx jest <path>\n")
    part = execution.project_notes(tmp_path)
    assert "npx jest <path>" in part and "/run/cache/notes.md" in part


def test_a_report_claiming_unchanged_files_goes_back_without_review(client, setup_project, tmp_path):
    from darkfactory.execution import handoff_files

    _, task = prepared(client, setup_project, tmp_path)

    class Claims(DeterministicExecutor):
        def __init__(self):
            super().__init__()
            self.runs = []

        def run(self, task, project, connection, role, heartbeat, timeout):
            result = super().run(task, project, connection, role, heartbeat, timeout)
            self.runs.append(role)
            if role == "implement" and self.runs.count("implement") == 1:
                result["handoff_files"] = ["value.py", "src/missing.py"]
            return result

    executor = Claims()
    assert drive(Worker(executor), task["id"]).state == "merged"
    corrections = [e for e in events(task["id"]) if e.kind == "correction"]
    assert corrections and "src/missing.py" in corrections[0].message
    # The false claim cost a writer run, not a review run.
    assert executor.runs.index("review") > [i for i, r in enumerate(executor.runs) if r == "implement"][1]

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "handoff.json").write_text(
        json.dumps({"files": ["src/a.py: fix", "docs/: notes", "tests/*.py: more", "README.md"]})
    )
    assert handoff_files(evidence) == ["src/a.py", "README.md"]


def test_claims_check_accepts_renames_deletions_and_empty_changes(tmp_path):
    from types import SimpleNamespace

    from darkfactory.worker import unbacked_claims

    repo = SimpleNamespace(artifacts=tmp_path)
    (tmp_path / "diff.patch").write_text(
        "diff --git a/src/old.py b/src/new.py\nsimilarity index 90%\n"
        "diff --git a/docs/гайд.md b/docs/гайд.md\n"
    )
    assert unbacked_claims(repo, ["src/old.py", "src/new.py", "docs/гайд.md"]) is None
    assert "src/other.py" in unbacked_claims(repo, ["src/other.py"])
    (tmp_path / "diff.patch").write_text("\n")
    assert unbacked_claims(repo, ["src/other.py"]) is None
