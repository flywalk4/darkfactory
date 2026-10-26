"""A nearly full data disk pauses claiming after an early sweep and pages the owner once."""

from collections import namedtuple

from conftest import connect

from darkfactory import cleanup, db, disk
from darkfactory.notifications import NotificationChannel, NotificationDelivery

Usage = namedtuple("Usage", "total used free")


def channel():
    with db.transaction() as session:
        org_id = session.query(db.Organization.id).scalar()
        session.add(NotificationChannel(org_id=org_id, provider="rocket", name="ops", secret="x"))


def test_low_disk_sweeps_early_then_pauses_and_alerts_once(client, setup_project, monkeypatch):
    project, pool = setup_project
    connect(client, pool)
    free = {"value": 5}
    monkeypatch.setattr(
        disk.shutil, "disk_usage", lambda _path: Usage(100, 100 - free["value"], free["value"])
    )
    sweeps = []
    monkeypatch.setattr(cleanup, "sweep", lambda force=False, retention=None: sweeps.append(retention) or [])
    channel()

    current = disk.check(force=True)
    assert sweeps == [disk.EMERGENCY_RETENTION]
    assert current["paused"] and current["alert"]
    assert disk.paused()
    disk.check(force=True)
    with db.Session() as session:
        deliveries = session.query(NotificationDelivery).all()
        assert len(deliveries) == 1 and deliveries[0].task_id is None
        assert "диск почти заполнен" in deliveries[0].message
    assert client.get("/api/me").json()["disk"]["paused"]
    assert client.get("/api/notification-deliveries").status_code == 200

    free["value"] = 40
    assert not disk.check(force=True)["paused"]
    assert not client.get("/api/me").json()["disk"]["paused"]
