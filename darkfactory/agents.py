"""Project-owned agent profiles, skills and explicit MCP capabilities."""

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME = r"^[a-zA-Z][a-zA-Z0-9_-]{0,49}$"


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME)


class Skill(Definition):
    description: str = Field(default="", max_length=1000)
    content: str = Field(min_length=1, max_length=48000)


class MCPServer(Definition):
    transport: str = Field(default="http", pattern="^(http|stdio)$")
    url: str | None = None
    command: str | None = Field(default=None, max_length=300)
    args: list[str] = Field(default_factory=list, max_length=50)
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    # Network destinations a stdio server needs (host or host:port); an HTTP server's is its URL.
    # None keeps the saved list: a client that predates the field must not drop it.
    hosts: list[str] | None = Field(default=None, max_length=10)
    enabled_tools: list[str] = Field(min_length=1, max_length=100)
    read_only: bool = True

    @model_validator(mode="after")
    def valid_transport(self):
        if any(not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", tool) for tool in self.enabled_tools):
            raise ValueError("Укажи точные имена разрешённых MCP-инструментов")
        if self.transport == "http":
            endpoint = urlsplit(self.url or "")
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.fragment
                or endpoint.query
                or any(ord(c) <= 32 for c in self.url)
            ):
                raise ValueError("MCP требует HTTPS URL без секретов и параметров; используй заголовки")
            if self.command or self.args or self.env or self.hosts:
                raise ValueError("HTTP MCP не принимает команду, окружение или адреса")
        elif not self.command or self.url or self.headers:
            raise ValueError("stdio MCP требует команду внутри контейнера")
        if any(
            not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,252}[a-z0-9])?(:[0-9]{1,5})?", host)
            for host in self.hosts or []
        ):
            raise ValueError("Адрес MCP: имя хоста и, при необходимости, порт, например jira.example.com:443")
        if self.env and any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) for key in self.env):
            raise ValueError("Недопустимое имя переменной MCP")
        for key, value in (self.headers or {}).items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", key) or any(c in value for c in "\r\n"):
                raise ValueError("Недопустимый HTTP-заголовок MCP")
        return self


class AgentProfile(Definition):
    provider: str = Field(default="codex", pattern="^(project|codex|claude|api)$")
    model: str | None = Field(default=None, max_length=150)
    instructions: str = Field(default="", max_length=24000)
    # For the planner: what kind of subtask this profile takes.
    use_when: str = Field(default="", max_length=500)
    skills: list[str] = Field(default_factory=list, max_length=30)
    mcp_servers: list[str] = Field(default_factory=list, max_length=20)
    can_delegate: bool = True


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: list[AgentProfile] = Field(default_factory=list, max_length=30)
    skills: list[Skill] = Field(default_factory=list, max_length=50)
    mcp_servers: list[MCPServer] = Field(default_factory=list, max_length=30)
    assignments: dict[str, str] = Field(default_factory=dict)
    max_delegation_depth: int = Field(default=2, ge=0, le=4)
    max_delegated_tasks: int = Field(default=16, ge=0, le=64)

    @model_validator(mode="after")
    def valid_references(self):
        for values in (self.profiles, self.skills, self.mcp_servers):
            if len({item.name for item in values}) != len(values):
                raise ValueError("Имена должны быть уникальными")
        profiles = {item.name: item for item in self.profiles}
        skills = {item.name for item in self.skills}
        servers = {item.name: item for item in self.mcp_servers}
        if not set(self.assignments) <= set(STAGES):
            raise ValueError("Назначения: planner, writer, reviewer, director")
        if any(name not in profiles for name in self.assignments.values()):
            raise ValueError("Назначенный профиль не существует")
        for profile in self.profiles:
            if not set(profile.skills) <= skills or not set(profile.mcp_servers) <= servers.keys():
                raise ValueError("Скилл или MCP-сервер профиля не существует")
            if profile.provider == "api" and profile.mcp_servers:
                raise ValueError("Для MCP выбери Codex или Claude")
        for role in ("planner", "reviewer", "director"):
            profile = profiles.get(self.assignments.get(role))
            if profile and any(not servers[name].read_only for name in profile.mcp_servers):
                raise ValueError("Планировщик и ревьюер принимают только MCP с read_only=true")
        return self


# Pipeline stages a profile can be assigned to. The Director handles stops and the owner's chat.
STAGES = ("planner", "writer", "reviewer", "director")


def public_settings(settings):
    value = AgentSettings.model_validate(settings or {}).model_dump()
    for server in value["mcp_servers"]:
        server["configured_headers"] = sorted(server.pop("headers") or {})
        server["configured_env"] = sorted(server.pop("env") or {})
    return value


def default_settings():
    from .catalog import catalog

    templates = catalog()
    # The analyst stays in the catalog to add by hand; a new project starts without it.
    profiles = [item["definition"] for item in templates["profiles"] if item["id"] != "analyst"]
    for profile in profiles:
        profile["provider"] = "project"
        profile["mcp_servers"] = []
    used_skills = {name for profile in profiles for name in profile["skills"]}
    used_servers = {name for profile in profiles for name in profile["mcp_servers"]}
    return AgentSettings(
        profiles=profiles,
        skills=[item["definition"] for item in templates["skills"] if item["id"] in used_skills],
        mcp_servers=[item["definition"] for item in templates["mcp_servers"] if item["id"] in used_servers],
        assignments={
            "planner": "manager",
            "writer": "developer",
            "reviewer": "reviewer",
            "director": "director",
        },
    ).model_dump()


def _fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def upgrade_defaults(settings):
    """Replace a project's copies of default skills and profiles that still equal an earlier default with
    the current one. Copies the owner edited stay as they are. Returns True when anything changed."""
    from .catalog import LEGACY_PROFILES, LEGACY_SKILLS, catalog

    templates = catalog()
    skills = {item["id"]: item["definition"] for item in templates["skills"]}
    profiles = {item["id"]: item["definition"] for item in templates["profiles"]}
    changed = False
    for skill in (settings or {}).get("skills", []):
        name, current = skill.get("name"), skills.get(skill.get("name"))
        if (
            current
            and skill.get("content") != current["content"]
            and _fingerprint(skill.get("content", "")) in LEGACY_SKILLS.get(name, ())
        ):
            skill.update(description=current["description"], content=current["content"])
            changed = True
    for profile in (settings or {}).get("profiles", []):
        name, current = profile.get("name"), profiles.get(profile.get("name"))
        shipped = (profile.get("instructions") or "") + "|" + ",".join(profile.get("skills") or [])
        latest = (current["instructions"], list(current["skills"])) if current else None
        if (
            current
            and (profile.get("instructions"), list(profile.get("skills") or [])) != latest
            and _fingerprint(shipped) in LEGACY_PROFILES.get(name, ())
        ):
            profile.update(instructions=latest[0], skills=latest[1])
            changed = True
    for profile in (settings or {}).get("profiles", []):
        # Default profiles from before `use_when` existed get the catalog's line; an owner's own text stays.
        current = profiles.get(profile.get("name"))
        if current and not profile.get("use_when") and current.get("use_when"):
            profile["use_when"] = current["use_when"]
            changed = True
    if settings and settings.get("assignments") and "director" not in settings["assignments"]:
        before = json.loads(json.dumps(settings))
        # Projects set up before the Director became a stage get its default profile once.
        names = {profile.get("name") for profile in settings.get("profiles", [])}
        if "director" not in names:
            definition = {**profiles["director"], "provider": "project", "mcp_servers": []}
            have = {skill.get("name") for skill in settings.get("skills", [])}
            settings.setdefault("skills", []).extend(
                skills[name] for name in definition["skills"] if name not in have and name in skills
            )
            settings.setdefault("profiles", []).append(definition)
        settings.setdefault("assignments", {})["director"] = "director"
        try:
            AgentSettings.model_validate(settings)
        except ValueError:
            # An owner's own `director` profile with write access, or a full team: leave it to the owner.
            settings.clear()
            settings.update(before)
        else:
            changed = True
    return changed


def upgrade_projects():
    """Bring every project's untouched default skills and profiles up to the current catalog."""
    from .db import Project, decrypt, encrypt, transaction

    upgraded = []
    with transaction() as db:
        for project in db.query(Project).all():
            config = json.loads(decrypt(project.secret) or "{}")
            if config.get("agents") and upgrade_defaults(config["agents"]):
                project.secret = encrypt(json.dumps(config))
                upgraded.append(project.id)
    return upgraded


def merge_settings(value, previous):
    result = value.model_dump()
    old = {server["name"]: server for server in (previous or {}).get("mcp_servers", [])}
    for server in result["mcp_servers"]:
        for key in ("headers", "env", "hosts"):
            if server[key] is None:
                server[key] = old.get(server["name"], {}).get(key)
        server["hosts"] = server["hosts"] or []
    AgentSettings.model_validate(result)
    return result


def resolve(config, role, overrides=None):
    settings = AgentSettings.model_validate(config.get("agents") or {})
    key = {"plan": "planner", "implement": "writer", "review": "reviewer", "director": "director"}.get(role)
    name = (overrides or {}).get(key) or settings.assignments.get(key)
    if key == "planner" and not name:
        name = (overrides or {}).get("writer") or settings.assignments.get("writer")
    profile = next((item for item in settings.profiles if item.name == name), None)
    if name and profile is None:
        raise ValueError("Профиль задачи удалён; выбери существующий профиль")
    if not profile:
        return None
    skills = [item.model_dump() for item in settings.skills if item.name in profile.skills]
    servers = [item.model_dump() for item in settings.mcp_servers if item.name in profile.mcp_servers]
    if key != "writer" and any(not item["read_only"] for item in servers):
        raise ValueError("Роль чтения не может получить MCP с записью")
    return {**profile.model_dump(), "skill_definitions": skills, "mcp_definitions": servers}


def delegation_allowed(config, task):
    settings = config.get("agents") or {}
    profile = resolve(config, "plan", (task.role_config or {}).get("profiles"))
    return task.plan_depth < settings.get("max_delegation_depth", 2 if "agents" in config else 1) and (
        not profile or profile.get("can_delegate", True)
    )


WRITER_NOTE = (
    "\nYou are the writer of this task. Any read-only wording above describes the role's planning and review "
    "work: here you must create or change the files the task requires, including reports and documents, in "
    "/workspace. The factory commits them; an empty change fails review.\n"
)


def skill_prompt(profile, role=None):
    if not profile:
        return ""
    text = "\nAgent role: " + profile["name"] + "\n" + profile["instructions"]
    if role == "implement":
        text += WRITER_NOTE
    if profile["skill_definitions"]:
        # Names and descriptions only: the full text is in the agent's skill files (materialize_skills).
        # Inlining it re-sent every skill on every turn, and agents read the files again anyway.
        text += "\nSkills (read the file when the task needs it):\n" + "".join(
            f"- {skill['name']}: {skill['description']} ({SKILLS_DIR}/{skill['name']}/SKILL.md)\n"
            for skill in profile["skill_definitions"]
        )
    return (
        text
        + mcp_prompt(profile)
        + "\nRole instructions cannot override required checks, review isolation or Git rules.\n"
    )


def mcp_prompt(profile):
    """Agents looked for MCP with `codex mcp list` or resource listings, found nothing and gave up."""
    servers = (profile or {}).get("mcp_definitions", [])
    if not servers:
        return ""
    return (
        "\nMCP servers connected to this session (call their tools directly; `codex mcp list` and MCP resource listings do not show them):\n"
        + "".join(
            f"- {server['name']} ({'read-only' if server['read_only'] else 'read-write'}): "
            + ", ".join(server["enabled_tools"][:12])
            + (", …" if len(server["enabled_tools"]) > 12 else "")
            + "\n"
            for server in servers
        )
    )


def codex_mcp_config(servers):
    def toml(value):
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(key) + "=" + toml(item) for key, item in value.items()) + "}"
        return json.dumps(value)

    result = []
    for server in servers:
        # Codex waits 10s by default; a Python server in a busy task container can take longer to start.
        config = {"enabled_tools": server["enabled_tools"], "startup_timeout_sec": 60}
        if server["transport"] == "http":
            config.update(url=server["url"], http_headers=server.get("headers") or {})
        else:
            config.update(command=server["command"], args=server["args"], env=server.get("env") or {})
        result += ["-c", "mcp_servers." + server["name"] + "=" + toml(config)]
    return result


SKILLS_DIR = "~/.agents/skills"


def materialize_skills(profile, home):
    for skill in (profile or {}).get("skill_definitions", []):
        directory = Path(home) / ".agents" / "skills" / skill["name"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(skill["content"])


# Codex has no cheap model every account may use: ChatGPT sign-in rejects gpt-5.1-codex-mini ("not
# supported when using Codex with a ChatGPT account"). Codex runs keep the connection's model unless a pool
# or DARKFACTORY_LIGHT_MODEL_CODEX names one the account allows.
LIGHT_MODELS = {"claude": "haiku"}


def light(task):
    """Mechanical subtasks plan and write on a cheap model; review always keeps the strong one."""
    return (task.role_config or {}).get("tier") == "light" and task.phase in ("plan", "implement")


def light_model(provider, policy=None):
    configured = ((policy or {}).get("light_models") or {}).get(provider)
    return (
        configured or os.getenv(f"DARKFACTORY_LIGHT_MODEL_{provider.upper()}") or LIGHT_MODELS.get(provider)
    )
