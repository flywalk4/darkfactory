"""Public registries are served from local caching mirrors unless the project picks its own registry."""

from darkfactory import mirrors


def test_mirrors_replace_public_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFACTORY_NPM_MIRROR", "http://npm-cache:4873/")
    monkeypatch.setenv("DARKFACTORY_PYPI_MIRROR", "http://pypi-cache:5000/index/")
    allowed, env = mirrors.configure(tmp_path)
    assert allowed == {("npm-cache", 4873), ("pypi-cache", 5000)}
    assert env["npm_config_registry"] == "http://npm-cache:4873/"
    assert (env["PIP_INDEX_URL"], env["PIP_TRUSTED_HOST"]) == ("http://pypi-cache:5000/index/", "pypi-cache")


def test_project_registry_wins_and_empty_setting_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFACTORY_NPM_MIRROR", "http://npm-cache:4873/")
    monkeypatch.setenv("DARKFACTORY_PYPI_MIRROR", "http://pypi-cache:5000/index/")
    (tmp_path / ".npmrc").write_text("registry=https://npm.corp.example/\n")
    (tmp_path / "requirements.txt").write_text("--index-url https://pypi.corp.example/simple\nrequests\n")
    assert mirrors.configure(tmp_path) == (set(), {})
    monkeypatch.setenv("DARKFACTORY_NPM_MIRROR", "")
    monkeypatch.delenv("DARKFACTORY_PYPI_MIRROR")
    assert mirrors.configure(tmp_path / "missing") == (set(), {})
