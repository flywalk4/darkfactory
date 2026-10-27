from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import connect

from darkfactory import db, schedules


def at(text, tz="Europe/Moscow"):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz)).timestamp()


def local(stamp, tz="Europe/Moscow"):
    return datetime.fromtimestamp(stamp, ZoneInfo(tz)).strftime("%a %Y-%m-%d %H:%M")


@pytest.mark.parametrize(
    "cron, start, expected",
    [
        ("0 */4 * * *", "2026-10-26 13:10", "Mon 2026-10-26 16:00"),
        ("30 9 * * 1-5", "2026-10-30 10:00", "Mon 2026-11-02 09:30"),  # Friday after 9:30: next Monday.
        ("0 9 * * 1-5", "2026-10-26 08:59", "Mon 2026-10-26 09:00"),
        ("0 0 1 * *", "2026-12-15 00:00", "Fri 2027-01-01 00:00"),
        ("*/15 * * * *", "2026-10-26 13:00", "Mon 2026-10-26 13:15"),  # Strictly after the given minute.
        ("@weekly", "2026-10-26 13:00", "Sun 2026-11-01 00:00"),
        ("0 9 13 * 5", "2026-10-26 13:00", "Fri 2026-10-30 09:00"),  # Day and weekday: either one.
        ("0 12 * * 7", "2026-10-26 13:00", "Sun 2026-11-01 12:00"),  # 7 is Sunday too.
    ],
)
def test_next_time_follows_cron_in_the_schedule_timezone(cron, start, expected):
    assert local(schedules.next_time(cron, at(start), "Europe/Moscow")) == expected


@pytest.mark.parametrize(
    "cron", ["", "* * * *", "61 * * * *", "0 25 * * *", "a b c d e", "0 0 31 2 *", "*/0 * * * *"]
)
def test_invalid_cron_is_refused(cron):
    with pytest.raises(ValueError):
        schedules.next_time(cron, at("2026-10-26 13:00"), "UTC")


def test_old_timers_become_schedules():
    assert [schedules.hours_to_cron(h) for h in (0, 1, 4, 6, 24, 48)] == [
        None,
        "0 * * * *",
        "0 */4 * * *",
        "0 */6 * * *",
        "0 3 * * *",
        "0 3 * * *",
    ]


def make_due(schedule_id):
    with db.transaction() as session:
        session.get(schedules.Schedule, schedule_id).next_at = 1


def test_owner_manages_schedules_and_a_task_schedule_creates_one_task_at_a_time(client, setup_project):
    project, pool = setup_project
    connect(client, pool)
    seeded = client.get(f"/api/projects/{project['id']}/schedules").json()
    assert sorted(item["kind"] for item in seeded["items"]) == ["backlog", "main_sync"]

    base = f"/api/projects/{project['id']}/schedules"
    assert (
        client.post(
            base, json={"kind": "task", "cron": "61 * * * *", "title": "x", "request": "y"}
        ).status_code
        == 400
    )
    assert client.post(base, json={"kind": "task", "cron": "0 9 * * 1", "title": "Аудит"}).status_code == 400
    bad_zone = {"kind": "main_sync", "cron": "0 * * * *", "timezone": "Mars/Base"}
    assert client.post(base, json=bad_zone).status_code == 400
    created = client.post(
        base,
        json={
            "kind": "task",
            "cron": "0 9 * * 1",
            "timezone": "Europe/Moscow",
            "title": "Еженедельный аудит зависимостей",
            "request": "Обнови зависимости с уязвимостями; проверки должны пройти.",
            "priority": "low",
        },
    )
    assert created.status_code == 201, created.text
    schedule = created.json()
    assert local(schedule["next_at"]).startswith("Mon") and local(schedule["next_at"]).endswith("09:00")

    make_due(schedule["id"])
    assert schedule["id"] in schedules.tick(force=True)
    with db.Session() as session:
        row = session.get(schedules.Schedule, schedule["id"])
        task = session.get(db.Task, row.last_task_id)
        assert row.next_at > 10**9
    assert task.title == "Еженедельный аудит зависимостей" and task.priority == "low"
    assert task.role_config == {"schedule": schedule["id"]} and task.state == "queued"

    # The previous run's task is still open: the schedule does not stack another one.
    again = client.post(f"/api/schedules/{schedule['id']}/run").json()
    assert "не закончена" in again["last_result"] and again["last_task_id"] == task.id

    off = client.put(f"/api/schedules/{schedule['id']}", json={**schedule, "enabled": False}).json()
    assert off["next_at"] is None
    make_due(schedule["id"])
    with db.transaction() as session:
        session.get(schedules.Schedule, schedule["id"]).enabled = False
    assert schedule["id"] not in schedules.tick(force=True)
    assert client.delete(f"/api/schedules/{schedule['id']}").status_code == 200
    assert client.post(f"/api/schedules/{schedule['id']}/run").status_code == 404


def test_main_sync_schedule_requests_a_sync(client, setup_project):
    project, _ = setup_project
    items = client.get(f"/api/projects/{project['id']}/schedules").json()["items"]
    sync = next(item for item in items if item["kind"] == "main_sync")
    result = client.post(f"/api/schedules/{sync['id']}/run").json()
    assert result["last_result"] == "Синхронизация main поставлена в очередь"
    with db.Session() as session:
        assert session.get(db.Project, project["id"]).health["main_sync_requested"]


def test_observer_cannot_change_schedules_and_deleting_project_removes_them(client, setup_project):
    project, _ = setup_project
    token = client.post("/api/tokens", json={"role": "observer"}).json()["token"]
    items = client.get(f"/api/projects/{project['id']}/schedules").json()["items"]
    headers = {"Authorization": "Bearer " + token}
    assert client.get(f"/api/projects/{project['id']}/schedules", headers=headers).status_code == 200
    assert client.delete(f"/api/schedules/{items[0]['id']}", headers=headers).status_code == 403
    assert client.post(f"/api/schedules/{items[0]['id']}/run", headers=headers).status_code == 403
    assert client.delete(f"/api/projects/{project['id']}").status_code == 200
    with db.Session() as session:
        assert not session.query(schedules.Schedule).filter_by(project_id=project["id"]).count()


def test_auto_promote_schedule_needs_the_setting(client, setup_project):
    project, _ = setup_project
    base = f"/api/projects/{project['id']}/schedules"
    schedule = client.post(base, json={"kind": "auto_promote", "cron": "0 * * * *"}).json()
    assert "выключен" in client.post(f"/api/schedules/{schedule['id']}/run").json()["last_result"]
    client.put(
        f"/api/projects/{project['id']}",
        json={
            "name": "Pilot",
            "remote": project["remote"],
            "checks": project["checks"],
            "auto_promote_hours": 6,
        },
    )
    started = client.post(f"/api/schedules/{schedule['id']}/run").json()
    assert started["queued"]
    jobs = client.get(base).json()["jobs"]
    assert jobs["auto_promote"]["requested"]


def test_parse_is_cached_and_sparse_cron_skips_idle_months():
    schedules.parse.cache_clear()
    start = datetime(2026, 2, 1, tzinfo=ZoneInfo("UTC")).timestamp()
    first = schedules.next_time("0 0 1 12 *", start)
    assert datetime.fromtimestamp(first, ZoneInfo("UTC")) == datetime(2026, 12, 1, tzinfo=ZoneInfo("UTC"))
    for _ in range(5):
        schedules.next_time("0 0 1 12 *", start)
    info = schedules.parse.cache_info()
    assert (info.misses, info.hits) == (1, 5)


def test_branch_jobs_name_the_seed_branch(client, setup_project):
    project, _ = setup_project
    with db.transaction() as session:
        session.get(db.Project, project["id"]).seed_branch = "nolang"
    items = client.get(f"/api/projects/{project['id']}/schedules").json()["items"]
    sync = next(item for item in items if item["kind"] == "main_sync")
    result = client.post(f"/api/schedules/{sync['id']}/run").json()
    assert result["last_result"] == "Синхронизация nolang поставлена в очередь" and result["queued"]
