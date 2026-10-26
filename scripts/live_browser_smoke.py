"""Real Codex/browser pilot in Compose worker; resumes saved task, never retries budgets.

CODEX_CONNECTION_ID selects an existing authorized subscription. No fixture model is used.
Run: docker compose exec -T -e CODEX_CONNECTION_ID=... worker python - < scripts/live_browser_smoke.py
"""

import fcntl
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import closing
from pathlib import Path

import docker
import httpx

from darkfactory.catalog import catalog
from darkfactory.execution import scoped_mount

HTML = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Browser pilot</title><style>
body{font-family:system-ui;margin:24px;background:#f4f6f9;color:#172238}
main{width:600px;margin:auto;padding:24px;background:white;border-radius:16px}
label{display:block;margin:16px 0}input{display:block;padding:12px;width:100%}
button{padding:12px 24px}#result{margin-top:16px}
</style><main><h1>Заявка</h1><form novalidate>
<label>Имя<input name="name" autocomplete="name"></label>
<label>Email<input name="email" autocomplete="email"></label>
<button>Отправить</button></form><p id="result" role="status"></p></main>
<script>
document.querySelector('form').addEventListener('submit', event => {
  event.preventDefault();
  const result = document.querySelector('#result');
  result.setAttribute('role', 'status');
  result.textContent = 'Заявка принята';
});
</script></html>
"""

CONFIG = """const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './browser-tests', workers: 1, timeout: 15000,
  outputDir: process.env.DARKFACTORY_EVIDENCE_DIR + '/test-results',
  reporter: [['line'], ['json', { outputFile: process.env.DARKFACTORY_EVIDENCE_DIR + '/results.json' }]],
  use: { baseURL: 'http://127.0.0.1:8765', screenshot: 'on', trace: 'on',
    launchOptions: { executablePath: '/usr/local/bin/darkfactory-chromium',
      args: ['--no-sandbox', '--disable-dev-shm-usage'] } },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 } } }
  ],
  webServer: { command: 'python3 -m http.server 8765 --bind 127.0.0.1',
    url: 'http://127.0.0.1:8765', reuseExistingServer: false, timeout: 15000 }
});
"""

TESTS = """const { test, expect } = require('@playwright/test');
test.beforeEach(async ({ page }) => { await page.goto('/'); });
test('empty form is rejected', async ({ page }) => {
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByRole('alert')).toHaveText('Заполни имя и корректный email');
});
test('invalid email is rejected', async ({ page }) => {
  await page.getByLabel('Имя').fill('Вадим');
  await page.getByLabel('Email', { exact: true }).fill('invalid');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByRole('alert')).toHaveText('Заполни имя и корректный email');
});
test('valid submit and resubmit', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.getByLabel('Имя').fill('Вадим');
  await page.getByLabel('Email', { exact: true }).fill('vadim@example.invalid');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByRole('status')).toHaveText('Заявка принята');
  await page.getByLabel('Email', { exact: true }).fill('');
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.getByRole('alert')).toHaveText('Заполни имя и корректный email');
  expect(errors).toEqual([]);
});
test('responsive layout', async ({ page }) => {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(page.getByRole('button', { name: 'Отправить' })).toBeInViewport();
});
"""


def git(*args, cwd=None):
    return subprocess.check_output(
        ["git", "-c", "user.name=Browser pilot", "-c", "user.email=pilot@example.invalid", *args],
        cwd=cwd,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    ).strip()


def main():
    lock = Path("/data/live-browser-smoke.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    checkpoint = Path("/data/live-browser-smoke.json")
    connection_id = os.environ["CODEX_CONNECTION_ID"]
    state = (
        json.loads(checkpoint.read_text())
        if checkpoint.exists()
        else {
            "run_id": uuid.uuid4().hex[:8],
            "connection_id": connection_id,
        }
    )
    assert state["connection_id"] == connection_id

    def save():
        temporary = checkpoint.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2))
        temporary.replace(checkpoint)

    save()
    root = Path("/data/live-browser") / state["run_id"]
    root.mkdir(parents=True, exist_ok=True)
    bare = Path("/repos") / ("live-browser-" + state["run_id"] + ".git")
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

        def request(method, path, body=None):
            response = api.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        def browser_check(workspace, stage):
            evidence = root / stage
            evidence.mkdir(exist_ok=True)
            evidence.chmod(0o777)
            container = client.containers.run(
                "darkfactory-executor:local",
                ["playwright test --config=playwright.config.cjs"],
                entrypoint=["sh", "-c"],
                detach=True,
                mounts=[
                    scoped_mount(str(workspace.relative_to("/data")), "/workspace", True),
                    scoped_mount(str(evidence.relative_to("/data")), "/run/evidence"),
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
                environment={"DARKFACTORY_EVIDENCE_DIR": "/run/evidence", "HOME": "/tmp/home"},
                tmpfs={"/tmp": "rw,nosuid,size=512m,mode=1777"},
                labels={"darkfactory.browser-pilot": state["run_id"]},
            )
            try:
                code = container.wait(timeout=180)["StatusCode"]
                (evidence / "container.log").write_bytes(container.logs())
                return code
            finally:
                container.remove(force=True)

        connection = next(c for c in request("GET", "/connections") if c["id"] == connection_id)
        assert connection["provider"] == "codex" and connection["pool_ids"]
        pool_id = connection["pool_ids"][0]
        pool = next(p for p in request("GET", "/pools") if p["id"] == pool_id)
        assert pool["allowed_providers"] == ["codex"]
        seed = root / "seed"
        if not bare.exists():
            seed.mkdir(exist_ok=True)
            (seed / "browser-tests").mkdir(exist_ok=True)
            (seed / "index.html").write_text(HTML)
            (seed / "favicon.ico").write_bytes(b"")
            (seed / "playwright.config.cjs").write_text(CONFIG)
            (seed / "browser-tests/ui.spec.cjs").write_text(TESTS)
            git("init", "-b", "main", cwd=seed)
            git("add", ".", cwd=seed)
            git("commit", "-m", "Seed failing browser scenarios", cwd=seed)
            git("clone", "--bare", str(seed), str(bare))
        if not state.get("baseline_failed"):
            assert browser_check(seed, "baseline") != 0, "Seed must reproduce defects"
            report = json.loads((root / "baseline/results.json").read_text())
            assert report["stats"]["unexpected"] > 0, "Failure must come from browser assertions"
            state["baseline_failed"] = True
            save()
        if "project_id" not in state:
            project = request(
                "POST",
                "/projects",
                {
                    "name": "Live browser " + state["run_id"],
                    "remote": str(bare),
                    "writer": "codex",
                    "reviewer": "codex",
                    "writer_pool": pool_id,
                    "reviewer_pool": pool_id,
                    "checks": ["playwright test --config=playwright.config.cjs"],
                    "allow_paid_fallback": False,
                },
            )
            state.update(project_id=project["id"], main=git("rev-parse", "main", cwd=bare))
            save()
        if not state.get("agents_configured"):
            settings = {
                key: [item["definition"] for item in catalog()[key]]
                for key in ("profiles", "skills", "mcp_servers")
            }
            for profile in settings["profiles"]:
                profile["can_delegate"] = False
                if profile["name"] == "developer":
                    profile["skills"].append("browser-testing")
                    profile["mcp_servers"].append("project-browser")
            settings["assignments"] = {"planner": "manager", "writer": "developer", "reviewer": "reviewer"}
            request("PUT", "/projects/" + state["project_id"] + "/agents", settings)
            state["agents_configured"] = True
            save()
        if "task_id" not in state:
            task = request(
                "POST",
                "/projects/" + state["project_id"] + "/tasks",
                {
                    "title": "Browser validation and responsive form",
                    "idempotency_key": "live-browser-" + state["run_id"],
                    "request": "Fix index.html only. Empty name or invalid email must show role=alert "
                    "with text Заполни имя и корректный email. Valid submit shows role=status with "
                    "Заявка принята. A later invalid submit must show error again. Fix mobile overflow. "
                    "Preserve all existing tests and config. Use project-browser MCP against the local "
                    "server in this container: inspect desktop 1440x1000 and mobile 390x844 and save "
                    "screenshots in /run/evidence. Run mandatory Playwright checks. No subtasks.",
                },
            )
            state["task_id"] = task["id"]
            save()
        previous = None
        for _ in range(1800):
            task = request("GET", "/tasks/" + state["task_id"])
            progress = (task["state"], task["phase"], task["reason"])
            if progress != previous:
                print(json.dumps({"task_id": task["id"], "progress": progress}), flush=True)
                previous = progress
            assert task["state"] not in {"needs_human", "cancelled", "review_required"}, progress
            if task["state"] == "merged":
                break
            time.sleep(2)
        else:
            raise TimeoutError("Existing task still active; resume this checkpoint")
        assert task["result_sha"] == task["verified_sha"] == task["reviewed_sha"]
        assert git("rev-parse", "preprod", cwd=bare) == task["result_sha"]
        assert git("rev-parse", "main", cwd=bare) == state["main"]
        assert git("diff", "--name-only", "main", "preprod", cwd=bare) == "index.html"
        accepted = root / "accepted"
        if accepted.exists():
            shutil.rmtree(accepted)
        git("clone", "--branch", "preprod", "--single-branch", str(bare), str(accepted))
        for name in ("playwright.config.cjs", "browser-tests/ui.spec.cjs"):
            assert (accepted / name).read_bytes() == (seed / name).read_bytes(), name
        assert browser_check(accepted, "independent-acceptance") == 0
        files = request("GET", "/tasks/" + task["id"] + "/evidence")["files"]
        assert any(item["name"].endswith(".png") for item in files)
        assert any(item["name"].endswith("results.json") for item in files)
        sessions = [item for item in files if item["name"].endswith("/session.md")]
        session_text = "\n".join(
            api.get("/tasks/" + task["id"] + "/evidence/" + item["name"]).raise_for_status().text
            for item in sessions
        )
        for tool in ("browser_navigate", "browser_resize", "browser_take_screenshot"):
            assert "### Tool call: " + tool in session_text, tool
        for width in (1440, 390):
            assert f'"width": {width}' in session_text, width
        state["result"] = {
            "status": "PASS",
            "real_provider": "codex",
            "real_browser": "chromium",
            "browser_mcp_session": True,
            "viewports": [1440, 390],
            "browser_tests": 8,
            "real_git": True,
            "main_unchanged": True,
            "independent_acceptance": True,
            "evidence_files": len(files),
        }
        save()
        print(json.dumps(state["result"]), flush=True)


if __name__ == "__main__":
    main()
