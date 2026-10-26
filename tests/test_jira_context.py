import json

import httpx

from darkfactory import db, jira
from darkfactory.notifications import NotificationChannel

JIRA = "https://jira.example.com"


def channel(project_id, org_id):
    config = {"base_url": JIRA, "token": "jira-secret-token", "project_keys": ["ABC"]}
    with db.transaction() as session:
        session.add(
            NotificationChannel(
                org_id=org_id,
                project_id=project_id,
                name="Jira",
                provider="jira",
                secret=db.encrypt(json.dumps(config)),
            )
        )


def task(project, title, request="Do it", parent=None):
    with db.transaction() as session:
        row = db.Task(
            org_id=project["org_id"],
            project_id=project["id"],
            title=title,
            request=request,
            branch="factory/x",
        )
        session.add(row)
        session.flush()
        if parent:
            session.add(db.Link(task_id=parent, other_id=row.id, kind="parent"))
        return row.id


def test_issue_reaches_prompt_from_parent_without_factory_comments_or_token(setup_project):
    project, _ = setup_project
    channel(project["id"], project["org_id"])
    parent = task(project, "ABC-7 Fix connectors")
    child = task(project, "Backend part", parent=parent)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer jira-secret-token"
        return httpx.Response(
            200,
            json={
                "fields": {
                    "summary": "Fix connectors",
                    "status": {"name": "В работе"},
                    "description": "Kafka settings must validate. jira-secret-token",
                    "comment": {
                        "comments": [
                            {"author": {"displayName": "Ann"}, "body": "Also WSDL import"},
                            {"author": {"displayName": "Bot"}, "body": "Darkfactory взяла задачу в работу"},
                        ]
                    },
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        text = jira.context(child, project["id"], client=client)
        assert jira.context(child, project["id"], client=client) == text
    assert calls == ["/rest/api/2/issue/ABC-7"]
    assert "Kafka settings must validate" in text and "Ann: Also WSDL import" in text
    assert "Darkfactory" not in text.split("Recent comments:")[1] and "jira-secret-token" not in text


def test_no_channel_or_no_issue_or_http_error_gives_nothing(setup_project):
    project, _ = setup_project
    plain = task(project, "Refactor parser")
    assert jira.context(plain, project["id"]) == ""
    channel(project["id"], project["org_id"])
    assert jira.context(plain, project["id"]) == ""
    linked = task(project, "ABC-8 Fix")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        assert jira.context(linked, project["id"], client=client) == ""
