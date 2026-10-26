import httpx
from conftest import make_task

from darkfactory import db, deploys, standqa

SHA = "a" * 40


def gitlab_project(client, setup_project, **extra):
    project, _ = setup_project
    response = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": project["name"],
            "remote": project["remote"],
            "checks": project["checks"],
            "forge": "gitlab",
            "forge_url": "https://gitlab.example.invalid/api/v4",
            "forge_repo": "team/app",
            "forge_token": "glpat-not-a-real-token",
            "deploy_job": "deploy",
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


class GitLab:
    """Pipeline 7 of SHA with an image build and the manual deploy job 70."""

    def __init__(self):
        self.build = "running"
        self.deploy = "manual"
        self.played = []
        self.pipelines = [{"id": 7, "ref": "main", "web_url": "https://gitlab/p/7"}]

    def __call__(self, request):
        assert request.headers["PRIVATE-TOKEN"] == "glpat-not-a-real-token"
        path = request.url.raw_path.decode().split("?")[0].removeprefix("/api/v4/projects/team%2Fapp")
        if path == "/pipelines":
            assert request.url.params["sha"] == SHA
            return httpx.Response(200, json=self.pipelines)
        if path == "/pipelines/7/jobs":
            return httpx.Response(
                200,
                json=[
                    {"id": 60, "name": "container-images:backend", "stage": "build", "status": self.build},
                    {
                        "id": 61,
                        "name": "docs-check",
                        "stage": "verify",
                        "status": "failed",
                        "allow_failure": True,
                    },
                    {
                        "id": 70,
                        "name": "deploy",
                        "stage": "deploy",
                        "status": self.deploy,
                        "web_url": "https://gitlab/j/70",
                    },
                ],
            )
        if path == "/jobs/70/play" and request.method == "POST":
            self.played.append(70)
            self.deploy = "pending"
            return httpx.Response(200, json={"id": 70, "web_url": "https://gitlab/j/70"})
        if path == "/jobs/70":
            return httpx.Response(200, json={"id": 70, "status": self.deploy})
        if path == "/jobs/70/trace":
            return httpx.Response(200, text="helm: UPGRADE FAILED: timed out")
        if path == "/repository/branches/main":
            return httpx.Response(200, json={"commit": {"id": SHA}})
        return httpx.Response(404)


def request_deploy(project_id, task_id):
    with db.transaction() as session:
        project = session.get(db.Project, project_id)
        assert deploys.request(session, project, session.get(db.Task, task_id), SHA)


def deploy_state(project_id):
    with db.Session() as session:
        return deploys.state(session.get(db.Project, project_id))


def kinds(task_id):
    with db.Session() as session:
        return [row.kind for row in session.query(db.Event).filter_by(task_id=task_id).order_by(db.Event.id)]


def test_deploy_waits_for_build_plays_job_and_hands_over_to_stand_tester(client, setup_project):
    project = gitlab_project(client, setup_project, stand_url="https://studio.example.invalid/")
    task = make_task(client, project)
    request_deploy(project["id"], task["id"])
    gitlab = GitLab()
    http = httpx.Client(transport=httpx.MockTransport(gitlab))

    deploys.advance(force=True, client=http)
    assert deploy_state(project["id"])["state"] == "build"
    deploys.advance(force=True, client=http)
    # Images of the commit are still building: playing the job now would fail its preflight.
    assert not gitlab.played and deploy_state(project["id"])["state"] == "build"

    gitlab.build = "success"
    deploys.advance(force=True, client=http)
    assert gitlab.played == [70]
    assert deploy_state(project["id"])["job_url"] == "https://gitlab/j/70"

    gitlab.deploy = "success"
    deploys.advance(force=True, client=http)
    assert deploy_state(project["id"])["state"] == "succeeded"
    assert kinds(task["id"])[-3:] == ["deploy_queued", "deploy_started", "deploy_succeeded"]
    with db.Session() as session:
        qa = standqa.state(session.get(db.Project, project["id"]))
    assert qa["reason"] == "deploy" and qa["sha"] == SHA and qa["requested"]
    assert client.get("/api/projects").json()[0]["health"]["deploy"]["state"] == "succeeded"


def test_failed_build_or_job_reports_on_the_task(client, setup_project):
    project = gitlab_project(client, setup_project)
    task = make_task(client, project)
    request_deploy(project["id"], task["id"])
    gitlab = GitLab()
    gitlab.build = "failed"
    http = httpx.Client(transport=httpx.MockTransport(gitlab))
    deploys.advance(force=True, client=http)
    deploys.advance(force=True, client=http)
    assert deploy_state(project["id"])["state"] == "failed"
    assert "container-images:backend" in deploy_state(project["id"])["error"]
    assert not gitlab.played

    request_deploy(project["id"], task["id"])
    gitlab.build, gitlab.deploy = "success", "manual"
    for _ in range(3):
        deploys.advance(force=True, client=http)
    gitlab.deploy = "failed"
    deploys.advance(force=True, client=http)
    with db.Session() as session:
        failed = session.query(db.Event).filter_by(task_id=task["id"], kind="deploy_failed").all()[-1]
    assert "UPGRADE FAILED" in failed.message


def test_owner_deploys_main_tip_and_settings_require_gitlab(client, setup_project):
    project, _ = setup_project
    assert client.post(f"/api/projects/{project['id']}/deploy").status_code == 409
    refused = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": "Pilot",
            "remote": project["remote"],
            "checks": project["checks"],
            "forge": "github",
            "forge_url": "https://api.github.example.invalid",
            "forge_repo": "team/app",
            "deploy_job": "deploy",
        },
    )
    assert refused.status_code == 400

    gitlab_project(client, setup_project)
    http = httpx.Client(transport=httpx.MockTransport(GitLab()))
    with db.transaction() as session:
        state = deploys.deploy_main(session, session.get(db.Project, project["id"]), "owner", client=http)
    assert state["sha"] == SHA and state["state"] == "pipeline"


def test_project_without_merge_requests_deploys_through_its_remote(client, setup_project):
    project, _ = setup_project
    response = client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": "Pilot",
            "remote": "https://gitlab.example.invalid/team/app.git",
            "checks": project["checks"],
            "git_token": "glpat-not-a-real-token",
            "deploy_job": "deploy",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["forge"] == "git"
    http = httpx.Client(transport=httpx.MockTransport(GitLab()))
    with db.transaction() as session:
        row = session.get(db.Project, project["id"])
        assert deploys.gitlab(row)[:2] == ("https://gitlab.example.invalid/api/v4", "team/app")
        assert deploys.deploy_main(session, row, "owner", client=http)["sha"] == SHA


def test_gitlab_outage_retries_before_failing(client, setup_project):
    project = gitlab_project(client, setup_project)
    task = make_task(client, project)
    request_deploy(project["id"], task["id"])
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(502)))
    deploys.advance(force=True, client=http)
    current = deploy_state(project["id"])
    assert current["state"] == "pipeline" and current["failures"] == 1
