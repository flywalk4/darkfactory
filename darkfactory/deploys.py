"""GitLab deploys: after the owner promotes preprod to main, start the project's manual deploy job and follow it.

The factory never deploys on its own judgement: a deploy follows only the owner's main decision (or an explicit
owner request). It waits for the commit's pipeline and its build stage, plays the configured manual job, and
reports the outcome on the promoted task. A successful deploy asks the stand tester to verify the stand.
"""

import json
import logging
import time
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import select

from .db import Project, Task, decrypt, event, now, transaction

log = logging.getLogger(__name__)

CHECK_EVERY = 20
PIPELINE_WAIT = 900
BUILD_WAIT = 2 * 3600
JOB_WAIT = 3600
ACTIVE = ("pipeline", "build", "running")
# Jobs that have not reached a final state yet; "manual" and "skipped" never will on their own.
UNFINISHED = {
    "created",
    "pending",
    "running",
    "waiting_for_resource",
    "preparing",
    "scheduled",
    "waiting_for_callback",
}

_last = 0.0


def config(project):
    return json.loads(decrypt(project.secret) or "{}")


def gitlab(project):
    """(API base, repository path, token) of the project's GitLab. A project without MRs (forge "git") still
    deploys: the API sits beside its HTTPS remote and the Git token serves both."""
    settings = config(project)
    if project.forge == "gitlab" and project.forge_url and project.forge_repo:
        return project.forge_url.rstrip("/"), project.forge_repo, settings.get("forge_token", "")
    remote = urlsplit(project.remote)
    if project.forge != "git" or remote.scheme not in {"http", "https"} or not remote.hostname:
        return None
    path = remote.path.strip("/").removesuffix(".git")
    base = f"{remote.scheme}://{remote.netloc}/api/v4"
    return base, path, settings.get("forge_token") or settings.get("git_token", "")


def job_name(project):
    """The deploy job, when deploys are on: a GitLab project with a job name in its settings."""
    if not gitlab(project):
        return None
    return (config(project).get("deploy_job") or "").strip() or None


def state(project):
    return dict((project.health or {}).get("deploy") or {})


def save(project, **fields):
    project.health = {**(project.health or {}), "deploy": {**state(project), **fields, "updated": now()}}


def request(db, project, task, sha, actor=None):
    """Queue a deploy of main at `sha`; a newer request replaces one that has not started its job yet."""
    if not job_name(project) or not sha:
        return False
    current = state(project)
    if current.get("state") == "running" and current.get("sha") != sha:
        # The running job deploys an older commit; the new one follows once it ends.
        save(project, queued={"sha": sha, "task_id": task.id if task else None, "actor": actor})
        return True
    project.health = {
        **(project.health or {}),
        "deploy": {
            "state": "pipeline",
            "sha": sha,
            "task_id": task.id if task else None,
            "actor": actor,
            "requested": now(),
            "updated": now(),
        },
    }
    if task:
        event(db, task, "deploy_queued", f"Деплой {sha[:12]} ждёт пайплайн GitLab", commit=sha)
    return True


class Forge:
    def __init__(self, project, client=None):
        base, repository, token = gitlab(project)
        self.base = base + "/projects/" + quote(repository, safe="")
        self.headers = {"PRIVATE-TOKEN": token}
        self.client = client or httpx.Client(timeout=30)

    def get(self, path, **params):
        response = self.client.get(self.base + path, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()

    def post(self, path):
        response = self.client.post(self.base + path, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def trace(self, job_id, limit=3000):
        response = self.client.get(f"{self.base}/jobs/{job_id}/trace", headers=self.headers)
        return response.text[-limit:] if response.status_code == 200 else ""

    def jobs(self, pipeline_id):
        jobs, page = [], 1
        while page:
            response = self.client.get(
                f"{self.base}/pipelines/{pipeline_id}/jobs",
                headers=self.headers,
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            jobs.extend(response.json())
            page = int(response.headers.get("x-next-page") or 0)
        return jobs

    def main_head(self, branch):
        return self.get("/repository/branches/" + quote(branch, safe=""))["commit"]["id"]


def step(project, forge):
    """Advance one project's deploy by at most one GitLab state change; returns the fields to save."""
    current = state(project)
    name = job_name(project)
    started = current.get("requested", now())
    if current["state"] == "pipeline":
        pipelines = forge.get("/pipelines", sha=current["sha"], order_by="id", sort="desc")
        pipeline = next((item for item in pipelines if item.get("ref") == project.seed_branch), None)
        pipeline = pipeline or (pipelines[0] if pipelines else None)
        if not pipeline:
            if now() - started > PIPELINE_WAIT:
                return {"state": "failed", "error": "GitLab не создал пайплайн для коммита main"}
            return {}
        return {"state": "build", "pipeline_id": pipeline["id"], "pipeline_url": pipeline.get("web_url")}
    if current["state"] == "build":
        jobs = forge.jobs(current["pipeline_id"])
        deploy = next((job for job in jobs if job.get("name") == name), None)
        if deploy is None:
            return {"state": "failed", "error": f"В пайплайне нет job «{name}»"}
        if deploy["status"] != "manual":
            # Someone started it from GitLab already: follow that run.
            return {"state": "running", "job_id": deploy["id"], "job_url": deploy.get("web_url")}
        building = [job for job in jobs if job.get("stage") == "build" and job.get("name") != name]
        broken = [
            job["name"]
            for job in building
            if job["status"] in {"failed", "canceled"} and not job.get("allow_failure")
        ]
        if broken:
            return {"state": "failed", "error": "Сборка образов упала: " + ", ".join(sorted(broken)[:10])}
        if any(job["status"] in UNFINISHED for job in building):
            if now() - started > BUILD_WAIT:
                return {"state": "failed", "error": "Сборка образов не закончилась за 2 часа"}
            return {}
        played = forge.post(f"/jobs/{deploy['id']}/play")
        return {
            "state": "running",
            "job_id": played.get("id", deploy["id"]),
            "job_url": played.get("web_url") or deploy.get("web_url"),
            "started": now(),
        }
    if current["state"] == "running":
        job = forge.get(f"/jobs/{current['job_id']}")
        if job["status"] == "success":
            return {"state": "succeeded", "finished": now()}
        if job["status"] in {"failed", "canceled"}:
            return {
                "state": "failed",
                "error": f"Job {name}: {job['status']}",
                "trace": forge.trace(current["job_id"]),
            }
        if now() - current.get("started", started) > JOB_WAIT:
            return {"state": "failed", "error": f"Job {name} идёт дольше часа"}
        return {}
    return {}


def report(db, project, fields):
    """Record a finished deploy on the promoted task and hand a successful one to the stand tester."""
    from . import standqa

    current = state(project)
    task = db.get(Task, current.get("task_id") or "")
    sha = current.get("sha", "")
    link = current.get("job_url") or current.get("pipeline_url") or ""
    if fields.get("state") == "running" and task and current.get("state") != "running":
        event(db, task, "deploy_started", f"Запущен деплой {sha[:12]}: {link}".strip(), commit=sha, url=link)
    elif fields.get("state") == "succeeded":
        if task:
            event(
                db,
                task,
                "deploy_succeeded",
                f"Деплой {sha[:12]} на стенд завершён: {link}",
                commit=sha,
                url=link,
            )
        standqa.request(db, project, "deploy", sha=sha)
    elif fields.get("state") == "failed":
        message = f"Деплой {sha[:12]} не удался: {fields.get('error')}"
        if fields.get("trace"):
            message += "\n\nКонец лога job:\n" + fields["trace"][-1500:]
        if task:
            event(db, task, "deploy_failed", message + (f"\n{link}" if link else ""), commit=sha, url=link)
        log.warning("Deploy of %s failed: %s", project.id, fields.get("error"))


def advance(force=False, client=None):
    """Move every active deploy one step; returns the ids of projects whose deploy state changed."""
    global _last
    if not force and time.monotonic() - _last < CHECK_EVERY:
        return []
    _last = time.monotonic()
    with transaction() as db:
        projects = [row for row in db.scalars(select(Project)) if state(row).get("state") in ACTIVE]
        db.expunge_all()
    changed = []
    for project in projects:
        try:
            fields = step(project, Forge(project, client))
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            failures = state(project).get("failures", 0) + 1
            # A GitLab outage or a short network gap: retry a few times before giving up on the deploy.
            fields = {"failures": failures, "last_error": str(exc)[-300:]}
            if failures >= 15:
                fields.update(state="failed", error=f"GitLab недоступен: {str(exc)[-200:]}")
        if not fields:
            continue
        with transaction() as db:
            row = db.get(Project, project.id)
            if state(row).get("sha") != state(project).get("sha"):
                continue  # A newer request replaced this deploy meanwhile.
            report(db, row, fields)
            save(row, **fields)
            queued = state(row).get("queued")
            if fields.get("state") in {"succeeded", "failed"} and queued:
                task = db.get(Task, queued.get("task_id") or "")
                request(db, row, task, queued["sha"], queued.get("actor"))
        changed.append(project.id)
    return changed


def deploy_main(db, project, actor, client=None):
    """Owner's explicit request: deploy the current main tip."""
    sha = Forge(project, client).main_head(project.seed_branch)
    request(db, project, None, sha, actor)
    return state(project)
