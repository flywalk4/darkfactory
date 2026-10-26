import base64
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from conftest import make_task

from darkfactory import attachments, db, execution

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()


def test_task_attachments_are_stored_listed_served_and_scoped(client, setup_project):
    project, _ = setup_project
    task = make_task(
        client,
        project,
        attachments=[
            {"name": "screen shot.png", "media_type": "image/png", "data": PNG},
            {
                "name": "../../etc/spec.pdf",
                "media_type": "application/pdf",
                "data": base64.b64encode(b"%PDF").decode(),
            },
        ],
    )
    detail = client.get(f"/api/tasks/{task['id']}").json()
    names = [item["name"] for item in detail["attachments"]]
    assert names == ["01-screen_shot.png", "02-spec.pdf"]
    image = client.get(f"/api/tasks/{task['id']}/attachments/01-screen_shot.png")
    assert (
        image.headers["content-type"] == "image/png" and "sandbox" in image.headers["content-security-policy"]
    )
    pdf = client.get(f"/api/tasks/{task['id']}/attachments/02-spec.pdf")
    assert pdf.content == b"%PDF" and "attachment" in pdf.headers["content-disposition"]
    assert client.get(f"/api/tasks/{task['id']}/attachments/..%2F..%2Fdb.sqlite").status_code == 404

    added = client.post(
        f"/api/tasks/{task['id']}/attachments",
        json={
            "files": [
                {"name": "log.txt", "media_type": "text/plain", "data": base64.b64encode(b"x").decode()}
            ]
        },
    )
    assert added.status_code == 201 and added.json()["files"][-1]["name"] == "03-log.txt"
    fake = client.post(
        f"/api/tasks/{task['id']}/attachments",
        json={
            "files": [
                {"name": "x.png", "media_type": "image/png", "data": base64.b64encode(b"<svg>").decode()}
            ]
        },
    )
    assert fake.status_code == 422 and len(attachments.listing(task["id"])) == 3

    other = client.post("/api/organizations", json={"name": "Attach org"}).json()
    headers = {"Authorization": "Bearer " + other["token"]}
    assert client.get(f"/api/tasks/{task['id']}/attachments/03-log.txt", headers=headers).status_code == 404


def test_executor_mounts_attachments_and_sends_images(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = make_task(
        client, project, attachments=[{"name": "bug.png", "media_type": "image/png", "data": PNG}]
    )
    seen = []

    class Captured(Exception):
        pass

    def create(_image, **kwargs):
        job = db.ROOT / "jobs" / kwargs["labels"]["darkfactory.generation"] / "input.json"
        seen.append((json.loads(job.read_text()), kwargs["mounts"]))
        raise Captured()

    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(execution, "egress_proxy", lambda *args, **kwargs: nullcontext())
    with db.Session() as session, pytest.raises(Captured):
        execution.DockerExecutor().run(
            session.get(db.Task, task["id"]),
            session.get(db.Project, project["id"]),
            None,
            "implement",
            lambda: True,
            60,
        )
    job, mounts = seen[0]
    assert "/run/attachments/01-bug.png" in job["prompt"]
    assert job["images"] == [{"media_type": "image/png", "data": PNG}]
    mount = next(m for m in mounts if m["Target"] == "/run/attachments")
    assert mount["ReadOnly"] and mount["VolumeOptions"]["Subpath"] == f"artifacts/{task['id']}/attachments"


def test_resume_with_attachments_reopens_implementation(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    with db.transaction() as session:
        row = session.get(db.Task, task["id"])
        row.phase, row.state = "merge", "needs_human"
        row.candidate_sha = row.reviewed_sha = row.verified_sha = "a" * 40
    result = client.post(
        f"/api/tasks/{task['id']}/actions",
        json={
            "action": "resume",
            "attachments": [{"name": "bug.png", "media_type": "image/png", "data": PNG}],
        },
    ).json()
    assert result["phase"] == "implement" and result["state"] == "queued"
    assert [item["name"] for item in attachments.listing(task["id"])] == ["01-bug.png"]


def test_reviewer_sees_the_writers_browser_screenshots(client, setup_project, monkeypatch):
    project, _ = setup_project
    task = make_task(client, project)
    run = db.ROOT / "artifacts" / task["id"] / "browser" / "run1"
    run.mkdir(parents=True)
    (run / "execution.json").write_text(json.dumps({"role": "implement"}))
    (run / "panel-390.png").write_bytes(base64.b64decode(PNG))
    seen = []

    class Captured(Exception):
        pass

    def create(_image, **kwargs):
        job = db.ROOT / "jobs" / kwargs["labels"]["darkfactory.generation"] / "input.json"
        seen.append(json.loads(job.read_text()))
        raise Captured()

    monkeypatch.setattr(
        execution.docker, "from_env", lambda: SimpleNamespace(containers=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(execution, "egress_proxy", lambda *args, **kwargs: nullcontext())
    with db.Session() as session, pytest.raises(Captured):
        execution.DockerExecutor().run(
            session.get(db.Task, task["id"]),
            session.get(db.Project, project["id"]),
            None,
            "review",
            lambda: True,
            60,
        )
    assert seen[0]["images"] == [{"media_type": "image/png", "data": PNG}]
    assert "browser screenshots are attached" in seen[0]["prompt"]
