"""Real subscription pilot inside Compose worker; no fixture responses or paid API.

Set CODEX_CONNECTION_ID explicitly. Re-running resumes the saved pilot and task IDs.
The first task must merge before the other twelve tasks are submitted.
"""

import fcntl
import json
import os
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path

import docker
import httpx

from darkfactory.execution import scoped_mount


def git(*args, cwd=None):
    return subprocess.check_output(
        ["git", "-c", "user.name=Live pilot", "-c", "user.email=pilot@example.invalid", *args],
        cwd=cwd,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    ).strip()


def main():
    lock = Path("/data/live-codex-smoke.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    connection_id = os.environ["CODEX_CONNECTION_ID"]
    checkpoint = Path("/data/live-codex-smoke.json")
    state = (
        json.loads(checkpoint.read_text())
        if checkpoint.exists()
        else {
            "run_id": uuid.uuid4().hex[:8],
            "connection_id": connection_id,
            "projects": [],
            "tasks": {},
            "observed_containers": [],
        }
    )
    assert state["connection_id"] == connection_id, "Checkpoint belongs to another connection"

    def save():
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(checkpoint)

    save()
    root = Path("/repos") / ("live-codex-" + state["run_id"])
    root.mkdir(exist_ok=True)
    with (
        closing(docker.from_env()) as client,
        httpx.Client(
            base_url="http://app:8000/api",
            timeout=20,
            headers={
                "Authorization": "Bearer "
                + (os.getenv("DARKFACTORY_ADMIN_TOKEN") or Path("/data/bootstrap-token").read_text()).strip()
            },
        ) as api,
    ):

        def request(method, path, payload=None):
            response = api.request(method, path, json=payload)
            response.raise_for_status()
            return response.json()

        connection = next(c for c in request("GET", "/connections") if c["id"] == connection_id)
        assert connection["provider"] == "codex" and connection["pool_ids"]
        pool_id = connection["pool_ids"][0]
        pool = next(p for p in request("GET", "/pools") if p["id"] == pool_id)
        assert pool["allowed_providers"] == ["codex"], "Pilot requires a Codex-only pool"
        for number in range(len(state["projects"]) + 1, 3):
            work, bare = root / f"source-{number}", root / f"project-{number}.git"
            work.mkdir(exist_ok=True)
            (work / "tests").mkdir(exist_ok=True)
            for bug in range(1, 6):
                (work / f"calculator{bug}.py").write_text(
                    "def double(n):\n    return n * 2 if n >= 0 else n\n"
                )
            (work / "tests/test_common.py").write_text(
                "import unittest\n"
                + "\n".join(f"from calculator{i} import double as double{i}" for i in range(1, 6))
                + "\nclass Common(unittest.TestCase):\n    def test_positive(self):\n"
                + "".join(f"        self.assertEqual(double{i}(2), 4)\n" for i in range(1, 6))
            )
            git("init", "-b", "main", cwd=work)
            git("add", ".", cwd=work)
            git("commit", "-m", "Seed real model pilot", cwd=work)
            if not bare.exists():
                git("clone", "--bare", str(work), str(bare))
            project = request(
                "POST",
                "/projects",
                {
                    "name": f"Live Codex {state['run_id']} / {number}",
                    "remote": str(bare),
                    "writer": "codex",
                    "reviewer": "codex",
                    "writer_pool": pool_id,
                    "reviewer_pool": pool_id,
                    "checks": ["python3 -m unittest discover -s tests -v"],
                    "allow_paid_fallback": False,
                },
            )
            state["projects"].append(
                {"id": project["id"], "bare": str(bare), "main": git("rev-parse", "main", cwd=bare)}
            )
            save()

        specs = []
        for number, project in enumerate(state["projects"], 1):
            for bug in range(1, 6):
                specs.append(
                    (
                        project,
                        f"bug-{number}-{bug}",
                        {
                            "title": f"Bug {number}-{bug}: double negative numbers",
                            "request": f"Fix double(n) in calculator{bug}.py so it doubles negative integers as well as zero and positive integers. Add unittest regression coverage. Preserve existing checks. This is one small task; do not delegate subtasks.",
                        },
                    )
                )
            for feature in range(1, 3 if number == 1 else 2):
                specs.append(
                    (
                        project,
                        f"feature-{number}-{feature}",
                        {
                            "title": f"Feature {number}-{feature}: triple integers",
                            "request": f"Add feature{feature}.py exposing triple(n) that returns three times any integer, including negative integers and zero. Add unittest coverage. Preserve existing checks. This is one small task; do not delegate subtasks.",
                        },
                    )
                )

        def enqueue(spec):
            project, key, payload = spec
            if key not in state["tasks"]:
                task = request(
                    "POST",
                    f"/projects/{project['id']}/tasks",
                    {
                        **payload,
                        "idempotency_key": f"live-{state['run_id']}-{key}",
                    },
                )
                state["tasks"][key] = task["id"]
                save()

        observed = set(state["observed_containers"])

        def wait():
            previous = None
            deadline = time.monotonic() + 4 * 3600
            while time.monotonic() < deadline:
                ids = set(state["tasks"].values())
                for container in client.containers.list(filters={"label": "darkfactory.task"}):
                    attrs = container.attrs
                    if attrs["Config"]["Labels"].get("darkfactory.task") not in ids:
                        continue
                    host = attrs["HostConfig"]
                    assert (
                        host["NetworkMode"] == "none" and host["ReadonlyRootfs"] and "ALL" in host["CapDrop"]
                    )
                    assert all(
                        m["Destination"] not in {"/var/run/docker.sock", "/repos", "/data"}
                        for m in attrs["Mounts"]
                    )
                    observed.add(container.id)
                tasks = [request("GET", "/tasks/" + ident) for ident in state["tasks"].values()]
                progress = [(t["title"], t["state"], t["phase"]) for t in tasks]
                if progress != previous:
                    print(json.dumps({"run_id": state["run_id"], "tasks": progress}), flush=True)
                    state["observed_containers"] = sorted(observed)
                    save()
                    previous = progress
                failed = [
                    (t["title"], t["state"], t["reason"])
                    for t in tasks
                    if t["state"] in {"needs_human", "cancelled", "review_required"}
                ]
                assert not failed, failed
                if all(t["state"] == "merged" for t in tasks):
                    assert all(t["result_sha"] == t["verified_sha"] == t["reviewed_sha"] for t in tasks)
                    return
                time.sleep(2)
            raise TimeoutError("Pilot still active; inspect tasks before resuming")

        enqueue(specs[0])
        wait()
        for spec in specs[1:]:
            enqueue(spec)
        wait()
        assert len(state["tasks"]) == 13 and observed
        for number, project in enumerate(state["projects"], 1):
            assert git("rev-parse", "main", cwd=project["bare"]) == project["main"]
            folder = Path("/data/live-pilots") / state["run_id"] / str(number)
            folder.mkdir(parents=True, exist_ok=True)
            accepted = folder / "accepted"
            if not accepted.exists():
                git("clone", "--branch", "preprod", "--single-branch", project["bare"], str(accepted))
            oracle = folder / "oracle"
            oracle.mkdir(exist_ok=True)
            (oracle / "verify.py").write_text(
                "import importlib, sys\nsys.path.insert(0, '/workspace')\n"
                "for i in range(1, 6):\n    fn = importlib.import_module(f'calculator{i}').double\n    for n in [-100, -3, -1, 0, 1, 2, 100]:\n        assert fn(n) == n * 2, (i, n)\n"
                f"for i in range(1, {3 if number == 1 else 2}):\n    fn = importlib.import_module(f'feature{{i}}').triple\n    for n in [-100, -3, -1, 0, 1, 2, 100]:\n        assert fn(n) == n * 3, (i, n)\n"
            )
            client.containers.run(
                "darkfactory-executor:local",
                ["python3 -m unittest discover -s tests -v && python3 /acceptance/verify.py"],
                entrypoint=["sh", "-c"],
                mounts=[
                    scoped_mount(str(accepted.relative_to("/data")), "/workspace", True),
                    scoped_mount(str(oracle.relative_to("/data")), "/acceptance", True),
                ],
                user="1000:1000",
                working_dir="/workspace",
                network_mode="none",
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=256,
                mem_limit="2g",
                nano_cpus=2_000_000_000,
                remove=True,
                environment={"PYTHONDONTWRITEBYTECODE": "1"},
                tmpfs={"/tmp": "rw,nosuid,size=64m"},
            )
        state["result"] = {
            "status": "PASS",
            "tasks": 13,
            "bugs": 10,
            "features": 3,
            "real_provider": "codex",
            "real_git": True,
            "main_unchanged": True,
            "independent_acceptance": True,
            "repositories": str(root),
            "observed_containers": len(observed),
        }
        save()
        print(json.dumps(state["result"]), flush=True)


if __name__ == "__main__":
    main()
