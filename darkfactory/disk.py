"""A full data disk crashes PostgreSQL; the worker frees space first and stops taking work before that."""

import hashlib
import json
import logging
import os
import shutil
import time

from .db import ROOT, Organization, transaction

log = logging.getLogger(__name__)
INTERVAL = 60
EMERGENCY_RETENTION = 0
_last = 0.0


def pause_percent():
    return float(os.getenv("DARKFACTORY_DISK_PAUSE_PERCENT", "15"))


def alert_percent():
    return float(os.getenv("DARKFACTORY_DISK_ALERT_PERCENT", "10"))


def measure():
    usage = shutil.disk_usage(ROOT)
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "free_percent": 100 * usage.free / usage.total,
    }


def state():
    try:
        return json.loads((ROOT / "system" / "disk.json").read_text())
    except (OSError, ValueError):
        return {}


def paused():
    return bool(state().get("paused"))


def check(force=False):
    """Measure free space; below the pause threshold sweep finished workspaces and Docker garbage, then pause."""
    global _last
    if not force and time.monotonic() - _last < INTERVAL:
        return state()
    _last = time.monotonic()
    from . import cleanup

    current = measure()
    if current["free_percent"] < pause_percent():
        cleanup.sweep(force=True, retention=EMERGENCY_RETENTION)
        current = measure()
    previous = state()
    current.update(
        at=time.time(),
        paused=current["free_percent"] < pause_percent(),
        alert=current["free_percent"] < alert_percent(),
    )
    path = ROOT / "system" / "disk.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(current))
    if current["paused"] != bool(previous.get("paused")) or current["alert"] != bool(previous.get("alert")):
        level = "alert" if current["alert"] else "paused" if current["paused"] else "ok"
        log.warning("Data disk %s: %.1f%% free", level, current["free_percent"])
        if current["paused"]:
            notify(current)
    return current


def describe(current):
    free = current["free_bytes"] / 1024**3
    if current["alert"]:
        return f"Darkfactory · диск почти заполнен: свободно {free:.1f} ГБ ({current['free_percent']:.0f}%). Новые задачи не берутся; база может упасть. Освободи место в Docker."
    return f"Darkfactory · мало места на диске: свободно {free:.1f} ГБ ({current['free_percent']:.0f}%). Новые задачи на паузе, пока не освободится {pause_percent():.0f}%."


def notify(current):
    from .notifications import NotificationChannel, NotificationDelivery, subscribed

    level = "alert" if current["alert"] else "paused"
    key = hashlib.sha256(f"disk\0{level}\0{int(current['at'] // 3600)}".encode()).hexdigest()
    with transaction() as db:
        for org_id in [row.id for row in db.query(Organization.id).all()]:
            channels = db.query(NotificationChannel).filter_by(org_id=org_id, enabled=True).all()
            for channel in channels:
                if not subscribed(channel, "disk_low"):
                    continue
                if db.query(NotificationDelivery.id).filter_by(channel_id=channel.id, dedupe_key=key).first():
                    continue
                db.add(
                    NotificationDelivery(
                        channel_id=channel.id, task_id=None, dedupe_key=key, message=describe(current)
                    )
                )


def public():
    current = state()
    return {
        k: current[k]
        for k in ("free_bytes", "total_bytes", "free_percent", "paused", "alert")
        if k in current
    }
