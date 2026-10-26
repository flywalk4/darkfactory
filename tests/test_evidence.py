import os

import pytest
from conftest import make_task

from darkfactory import evidence
from darkfactory.db import ROOT


def test_evidence_authenticated_scoped_downloads_and_symlinks(client, setup_project):
    project, _ = setup_project
    task = make_task(client, project)
    directory = ROOT / "artifacts" / task["id"] / "browser" / "run-1"
    directory.mkdir(parents=True)
    (directory / "screen.png").write_bytes(b"real evidence bytes")
    (directory / "report.html").write_text("<script>alert(1)</script>")
    (directory / "secret.txt").symlink_to(ROOT / "bootstrap-token")
    (directory / "outside").symlink_to(ROOT, target_is_directory=True)
    base = f"/api/tasks/{task['id']}/evidence"
    listing = client.get(base).json()
    assert {file["name"] for file in listing["files"]} == {"run-1/screen.png", "run-1/report.html"}
    response = client.get(base + "/run-1/screen.png")
    assert response.content == b"real evidence bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    response = client.get(base + "/run-1/report.html")
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "sandbox" in response.headers["content-security-policy"]
    assert client.get(base + "/run-1/secret.txt").status_code == 404
    assert client.get(base + "/run-1/outside/bootstrap-token").status_code == 404
    assert client.get(base, headers={"Authorization": "Bearer wrong"}).status_code == 401
    other = client.post("/api/organizations", json={"name": "Another org"}).json()
    assert client.get(base, headers={"Authorization": "Bearer " + other["token"]}).status_code == 404
    assert (
        client.get(
            base + "/run-1/screen.png", headers={"Authorization": "Bearer " + other["token"]}
        ).status_code
        == 404
    )


def test_evidence_limits_and_rejects_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_FILE", 20)
    (tmp_path / "too-big").write_bytes(b"x" * 21)
    os.mkfifo(tmp_path / "pipe")
    for path in ("../secret", "/absolute", "a/../../secret", "a\\b", "pipe", "too-big"):
        with pytest.raises((ValueError, OSError)):
            evidence.read(tmp_path, path)
    assert evidence.listing(tmp_path)["files"] == []
    monkeypatch.setattr(evidence, "MAX_ENTRIES", 2)
    for i in range(5):
        (tmp_path / str(i)).write_text("small")
    listing = evidence.listing(tmp_path)
    assert len(listing["files"]) == 2 and listing["truncated"]
