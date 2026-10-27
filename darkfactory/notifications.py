"""Opt-in human notifications with a durable, leased delivery outbox.

Destinations belong to owner-managed channels, never to task input. Network calls
run outside database transactions. Delivery is at least once: a process crash
after a successful POST can cause a duplicate when its lease expires.
"""

import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx
from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    or_,
    update,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, Event, Project, Session, decrypt, now, transaction, uid
from .task_links import commit_url, public_asset, task_url

# A long task extends itself while it progresses, so "budget_warning" stays in the task history only.
IMPORTANT_KINDS = frozenset(
    {
        "clarification",
        "limit",
        "stopped",
        "execution_error",
        "needs_human",
        "accepted",
        "merged",
        "incident_detected",
        "incident_reopened",
        "human_message",
        "stalled",
        "stall_restart",
        "review_required",
        "network_blocked",
        "environment_blocked",
        "toolchain_failed",
        "token_warning",
        "supervisor_escalated",
        "network_unstable",
        "review_repeated",
        "disk_low",
        "deploy_succeeded",
        "deploy_failed",
        "qa_reported",
        "qa_not_fixed",
    }
)
# Stops the supervisor tries to resolve before paging the owner.
OWNER_STOP_KINDS = frozenset(
    {
        "clarification",
        "limit",
        "needs_human",
        "execution_error",
        "network_blocked",
        "environment_blocked",
        "review_repeated",
        "stopped",
    }
)
# Kinds channels saved before `known_events` existed could choose from; anything newer reaches them by default.
LEGACY_KINDS = frozenset(
    {
        "clarification",
        "limit",
        "stopped",
        "execution_error",
        "needs_human",
        "accepted",
        "merged",
        "incident_detected",
        "incident_reopened",
        "human_message",
    }
)
MAX_ATTEMPTS = 5
# Deliveries to a destination behind a VPN or gateway that is down retry without counting attempts.
UNREACHABLE = "Unreachable: "
UNREACHABLE_RETRY = 900
UNREACHABLE_DAYS = 3
LEASE_SECONDS = 90


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    provider: Mapped[str] = mapped_column(String(20))
    recipient: Mapped[str | None] = mapped_column(String(200))
    secret: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    events: Mapped[list] = mapped_column(JSON, default=lambda: sorted(IMPORTANT_KINDS))
    known_events: Mapped[list] = mapped_column(JSON, default=lambda: sorted(IMPORTANT_KINDS))
    # Next Telegram update to read for chat commands.
    command_offset: Mapped[int | None] = mapped_column(Integer)
    created: Mapped[float] = mapped_column(Float, default=now)


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("notification_channels.id"), index=True)
    # System alerts (disk) have no task.
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True, nullable=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"))
    dedupe_key: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    # The chat message a director answer replies to: Telegram message_id or Rocket.Chat room and thread.
    reply_to: Mapped[dict | None] = mapped_column(JSON)
    # Provider ID of the posted message, so a thread reply to it can answer the task.
    message_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[float] = mapped_column(Float, default=now, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(String(200))
    created: Mapped[float] = mapped_column(Float, default=now)
    sent_at: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("channel_id", "dedupe_key"),)


def command_config(credentials):
    """Optional chat commands: the API token they run as and the Rocket.Chat outgoing webhook token."""
    result = {}
    for key in ("director_token", "outgoing_token"):
        value = credentials.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{8,512}", value):
            raise ValueError("Command and outgoing webhook tokens must be 8-512 printable characters")
        result[key] = value
    senders = credentials.get("allowed_senders")
    if senders not in (None, "", []):
        if (
            not isinstance(senders, list)
            or len(senders) > 100
            or not all(
                isinstance(item, (str, int))
                and not isinstance(item, bool)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(item))
                for item in senders
            )
        ):
            raise ValueError("Allowed senders must be a list of chat user IDs")
        result["allowed_senders"] = sorted({str(item) for item in senders})
    return result


def validate_channel_config(provider, credentials):
    """Validate owner-supplied credentials without disclosing their contents."""
    if not isinstance(credentials, dict):
        raise ValueError("Notification credentials must be an object")
    commands = command_config(credentials)
    if provider == "rocketchat":
        url = credentials.get("webhook_url", "")
        if not isinstance(url, str) or len(url) > 4096:
            raise ValueError("Rocket.Chat requires an HTTPS webhook URL")
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and not any(char.isspace() for char in url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "Rocket.Chat requires an HTTPS webhook URL without user information or fragments"
            )
        # Corporate/private hosts are deliberate, owner-configured destinations.
        return {"webhook_url": url, **commands}
    if provider == "telegram":
        token = credentials.get("bot_token", "")
        chat = credentials.get("chat_id", "")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{8,}", token):
            raise ValueError("Telegram requires a bot token")
        if isinstance(chat, bool) or not isinstance(chat, (str, int)):
            raise ValueError("Telegram requires a chat ID or channel username")
        chat = str(chat).strip()
        if not re.fullmatch(r"-?[0-9]+|@[A-Za-z0-9_]{5,}", chat):
            raise ValueError("Telegram requires a chat ID or channel username")
        commands.pop("outgoing_token", None)
        return {"bot_token": token, "chat_id": chat, **commands}
    if provider == "jira":
        url = credentials.get("base_url", "")
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            valid = bool(
                parsed
                and parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() for char in url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Jira requires an HTTPS base URL without user information")
        result = {"base_url": url.rstrip("/")}
        token = credentials.get("token") or ""
        if not isinstance(token, str) or any(char.isspace() for char in token):
            raise ValueError("Jira token must be a single line")
        if token:
            result["token"] = token
        reviewer = credentials.get("reviewer") or ""
        if not isinstance(reviewer, str) or (reviewer and not re.fullmatch(r"[\w.@+-]{1,100}", reviewer)):
            raise ValueError("Jira reviewer must be a user name")
        transition = credentials.get("review_status") or ""
        if not isinstance(transition, str) or len(transition) > 100 or any(c in transition for c in "\r\n"):
            raise ValueError("Jira review status must be one line")
        keys = credentials.get("project_keys") or []
        if not isinstance(keys, list) or not all(
            isinstance(key, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,30}", key) for key in keys
        ):
            raise ValueError("Jira project keys look like ABC")
        people = credentials.get("assignee_fields") or []
        if not isinstance(people, list) or not all(
            isinstance(field, str) and re.fullmatch(r"customfield_[0-9]{1,9}", field) for field in people
        ):
            raise ValueError("Jira assignee fields look like customfield_13210")
        work = credentials.get("work_status") or ""
        if not isinstance(work, str) or len(work) > 100 or any(c in work for c in "\r\n"):
            raise ValueError("Jira work status must be one line")
        return {
            **result,
            "reviewer": reviewer,
            "review_status": transition,
            "work_status": work,
            "assignee_fields": people,
            "project_keys": keys,
        }
    raise ValueError("Unsupported notification provider")


# Progress only the issue's own thread wants: chat channels would turn these into noise.
JIRA_KINDS = frozenset({"created", "implementation_summary"})
# A finished task goes to the owner's review: the issue moves to the review status and to the reviewer.
# "accepted" is a merge into preprod, "merged" the later promotion to main.
JIRA_REVIEW_KINDS = frozenset({"review_required", "accepted", "merged"})


def jira_issue(task, config):
    """The Jira issue a task works on: a /browse/ link to this Jira, or a bare key of a listed project."""
    text = f"{task.title}\n{task.request}"
    link = re.search(re.escape(config["base_url"]) + r"/browse/([A-Z][A-Z0-9_]{1,30}-[0-9]{1,9})\b", text)
    if link:
        return link.group(1)
    for match in re.finditer(r"\b([A-Z][A-Z0-9_]{1,30})-[0-9]{1,9}\b", text):
        if match.group(1) in config["project_keys"]:
            return match.group(0)
    return None


def jira_token(config, project_id):
    """The channel's own token, else the project's Jira MCP token: one secret for reading and writing."""
    if config.get("token") or not project_id:
        return config.get("token")
    with Session() as db:
        project = db.get(Project, project_id)
        settings = json.loads(decrypt(project.secret) or "{}") if project else {}
    for server in (settings.get("agents") or {}).get("mcp_servers", []):
        token = (server.get("env") or {}).get("JIRA_PERSONAL_TOKEN")
        if server.get("command") == "mcp-atlassian" and token:
            return token
    return None


def public_channel(channel):
    return {
        "id": channel.id,
        "org_id": channel.org_id,
        "project_id": channel.project_id,
        "name": channel.name,
        "provider": channel.provider,
        "recipient": channel.recipient,
        "enabled": channel.enabled,
        "events": sorted(kind for kind in IMPORTANT_KINDS if subscribed(channel, kind)),
        "commands": "director_token" in json.loads(decrypt(channel.secret) or "{}"),
        "created": channel.created,
    }


def completion_summary(project, message):
    """Bound the executor's final comment and remove known project credentials first."""
    config = json.loads(decrypt(project.secret) or "{}")
    secrets = [config.get(key) for key in ("git_token", "forge_token", "ssh_private_key")]
    secrets.extend((config.get("test_env") or {}).values())
    body = str(message)
    for secret in sorted((item for item in secrets if isinstance(item, str) and item), key=len, reverse=True):
        body = body.replace(secret, "[redacted]")
    body = " ".join(body.split())
    return body[:347] + "…" if len(body) > 350 else body


def _completion_message(db, task, message):
    project = db.get(Project, task.project_id)
    previous = (
        db.query(Event)
        .filter(Event.task_id == task.id, Event.kind == "implementation_summary")
        .order_by(Event.id.desc())
        .first()
    )
    comment = completion_summary(project, previous.message if previous else message)
    lines = [f"Darkfactory · Выполнено: {task.title[:300]}", f"Проект: {project.name}", comment]
    link = commit_url(project, task)
    if link:
        lines.extend([f"Коммит: {task.result_sha}", link])
    lines.append("Задача: " + task_url(task))
    from .promotions import proposal

    if proposal(db, task):
        lines.append(f"Влить в {project.seed_branch} / отклонить: " + task_url(task) + "&promotion=1")
    return "\n".join(lines)


def subscribed(channel, kind):
    """Explicit choice for kinds the owner saw when saving the channel; kinds added later are on."""
    return kind in channel.events or kind not in (set(channel.known_events or ()) or LEGACY_KINDS)


def queue_notification(db, task, kind, message, event_id=None):
    """Queue important events in the caller's transaction; return queued count."""
    if kind not in IMPORTANT_KINDS and kind not in JIRA_KINDS:
        return 0
    if kind in {"accepted", "merged"} and task.state != "merged":
        return 0
    if kind in {"clarification", "limit", "needs_human", "execution_error"} and task.state != "needs_human":
        return 0
    if kind == "review_required" and task.state != "review_required":
        return 0
    if kind == "supervisor_escalated" and task.state != "needs_human":
        return 0
    if kind in OWNER_STOP_KINDS and task.state == "needs_human":
        from .director_stops import eligible, foldable

        if eligible(db, task) or foldable(db, task):
            return 0
    if kind == "stalled" and task.state != "running":
        return 0
    if kind == "stopped" and task.state not in {"needs_human", "cancelled", "failed", "stopped"}:
        return 0
    if kind == "created":
        body = (
            f"Darkfactory · подзадача: {task.title[:300]}\nКарточка: {task_url(task)}"
            if task.plan_depth
            else f"Darkfactory взяла задачу в работу: {task.title[:300]}\nКарточка: {task_url(task)}"
        )
    elif kind == "implementation_summary":
        body = (
            f"Darkfactory · сделано в этом прогоне: {task.title[:300]}\n"
            f"{completion_summary(db.get(Project, task.project_id), message)}\nКарточка: {task_url(task)}"
        )
    elif kind in {"accepted", "merged"}:
        body = _completion_message(db, task, message)
    elif kind in {"deploy_succeeded", "deploy_failed", "qa_reported", "qa_not_fixed"}:
        body = f"Darkfactory · {str(message)[:2500]}\nКарточка: {task_url(task)}"
    else:
        body = f"Darkfactory · {task.title[:300]}\nTask {task.id} · {task.state}\n{str(message)[:2000]}"
    key = (
        f"event:{event_id}"
        if event_id is not None
        else hashlib.sha256(f"{task.id}\0{kind}\0{task.state}\0{message}".encode()).hexdigest()
    )
    queued = 0
    channels = (
        db.query(NotificationChannel)
        .filter(
            NotificationChannel.org_id == task.org_id,
            NotificationChannel.enabled.is_(True),
            or_(NotificationChannel.project_id.is_(None), NotificationChannel.project_id == task.project_id),
        )
        .all()
    )
    for channel in channels:
        if kind in JIRA_KINDS:
            if channel.provider != "jira":
                continue
        elif not subscribed(channel, kind):
            continue
        reply_to = None
        if channel.provider == "jira":
            try:
                issue = jira_issue(task, validate_channel_config("jira", json.loads(decrypt(channel.secret))))
            except ValueError:
                issue = None
            if not issue:
                continue
            # A planned subtask shares its parent's issue: it comments, only the parent moves the issue.
            reply_to = {"issue": issue, "kind": kind, **({"subtask": True} if task.plan_depth else {})}
        if db.query(NotificationDelivery.id).filter_by(channel_id=channel.id, dedupe_key=key).first():
            continue
        db.add(
            NotificationDelivery(
                channel_id=channel.id,
                task_id=task.id,
                event_id=event_id,
                dedupe_key=key,
                message=body,
                reply_to=reply_to,
            )
        )
        queued += 1
    return queued


def _due(at):
    return or_(
        and_(NotificationDelivery.state == "pending", NotificationDelivery.retry_at <= at),
        and_(NotificationDelivery.state == "sending", NotificationDelivery.lease_until <= at),
    )


# Rocket.Chat shows markdown: a headline with an icon, bold field names and named links.
ROCKET_HEADLINES = [
    ("Darkfactory · Выполнено: ", "✅", "Выполнено"),
    ("Darkfactory взяла задачу в работу: ", "🛠️", "Взята в работу"),
    ("Darkfactory · подзадача: ", "🧩", "Подзадача"),
    ("Darkfactory · сделано в этом прогоне: ", "📝", "Сделано в этом прогоне"),
    ("Директор: ", "🧠", "Директор"),
]
ROCKET_LINKS = {"Карточка": "📋 Открыть карточку", "Задача": "📋 Открыть карточку", "Коммит": "🔗 Коммит"}
ROCKET_STATES = {
    "needs_human": "🙋 нужно твоё решение",
    "review_required": "👀 ждёт слияния человеком",
    "running": "⚙️ в работе",
    "queued": "⏳ в очереди",
    "merged": "✅ принята",
    "cancelled": "🚫 отменена",
    "failed": "❌ ошибка",
    "stopped": "⏸️ остановлена",
}
TASK_LINE = re.compile(r"^Task ([0-9a-f]{32}) · (\w+)$")


def rocketchat_text(body, commands=False):
    """Present a plain notification as Rocket.Chat markdown, keeping the task ID the webhook echo reads."""
    lines = body.split("\n")
    head, rest = lines[0], lines[1:]
    for prefix, icon, label in ROCKET_HEADLINES:
        if head.startswith(prefix):
            head = f"{icon} *{label}:* {head[len(prefix) :]}"
            break
    else:
        if head.startswith("Darkfactory · "):
            title = head[len("Darkfactory · ") :]
            head = f"⚠️ *{title}*" if not rest else f"🔔 *{title}*"
    out, waiting = [head], False
    for line in rest:
        task = TASK_LINE.match(line)
        name, sep, value = line.partition(": ")
        if task:
            waiting = task.group(2) in {"needs_human", "review_required"}
            out.append(f"`Task {task.group(1)}` · {ROCKET_STATES.get(task.group(2), task.group(2))}")
        elif sep and re.fullmatch(r"https?://\S+", value) and name in ROCKET_LINKS:
            out.append(f"[{ROCKET_LINKS[name]}]({value})")
        elif sep and re.fullmatch(r"https?://\S+", value):
            out.append(f"[{name}]({value})")
        elif sep and name in {"Проект", "Коммит"}:
            out.append(f"*{name}:* `{value}`" if name == "Коммит" else f"*{name}:* {value}")
        elif re.fullmatch(r"https?://\S+", line):
            out.append(f"[🔗 {urlsplit(line).hostname}]({line})")
        else:
            out.append(line)
    if waiting and commands:
        out += [
            "",
            "💬 _Ответь в треде этого сообщения — агент получит ответ и продолжит. `!help` — команды._",
        ]
    return "\n".join(out)


def _claim():
    owner = uid()
    with transaction() as db:
        at = now()
        db.execute(
            update(NotificationDelivery)
            .where(
                _due(at),
                NotificationDelivery.attempts >= MAX_ATTEMPTS,
            )
            .values(
                state="failed", lease_owner=None, lease_until=None, last_error="Delivery attempts exhausted"
            )
        )
        delivery = (
            db.query(NotificationDelivery)
            .filter(
                _due(at),
                NotificationDelivery.attempts < MAX_ATTEMPTS,
            )
            .order_by(NotificationDelivery.created, NotificationDelivery.id)
            .first()
        )
        if delivery is None:
            return None
        claimed = db.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == delivery.id,
                _due(at),
                NotificationDelivery.attempts < MAX_ATTEMPTS,
            )
            .values(
                state="sending",
                attempts=NotificationDelivery.attempts + 1,
                lease_owner=owner,
                lease_until=at + LEASE_SECONDS,
            )
        )
        if not claimed.rowcount:
            return None
        db.refresh(delivery)
        channel = db.get(NotificationChannel, delivery.channel_id)
        if channel is None or not channel.enabled:
            delivery.state = "skipped"
            delivery.lease_owner = None
            delivery.lease_until = None
            return {"skipped": True}
        return {
            "id": delivery.id,
            "owner": owner,
            "message": delivery.message,
            "provider": channel.provider,
            "recipient": channel.recipient,
            "secret": channel.secret,
            "reply_to": delivery.reply_to,
            "task_id": delivery.task_id,
            "project_id": channel.project_id,
        }


def _send(client, claimed):
    try:
        config = validate_channel_config(claimed["provider"], json.loads(decrypt(claimed["secret"])))
    except Exception:
        return "Invalid notification credentials", 0, True
    if claimed["provider"] == "jira":
        config["token"] = jira_token(config, claimed.get("project_id"))
        if not config["token"]:
            return "Jira token is not configured", 0, True
    body = claimed["message"]
    for value in config.values():
        if isinstance(value, str) and len(value) >= 8:
            body = body.replace(value, "[redacted]")
    reply_to = claimed.get("reply_to") or {}
    if claimed["provider"] == "jira":
        return _send_jira(client, config, reply_to, body)
    if claimed["provider"] == "telegram":
        url = f"https://api.telegram.org/bot{config['bot_token']}/sendMessage"
        payload = {"chat_id": config["chat_id"], "text": body, "link_preview_options": {"is_disabled": True}}
        if claimed.get("task_id") and config.get("director_token"):
            # The bot handles these buttons; without a command token nobody would answer them.
            task = claimed["task_id"]
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": "✍️ Ответить", "callback_data": "a:" + task},
                        {"text": "📋 Карточка", "callback_data": "t:" + task},
                    ]
                ]
            }
        if reply_to.get("message_id"):
            payload["reply_parameters"] = {
                "message_id": reply_to["message_id"],
                "allow_sending_without_reply": True,
            }
    else:
        url = config["webhook_url"]
        payload = {
            "text": rocketchat_text(body, commands=bool(config.get("director_token"))),
            "alias": "Darkfactory",
        }
        if avatar := public_asset("logo.png"):
            payload["avatar"] = avatar
        else:
            # Without a public address there is no image URL; an emoji avatar still avoids the grey default.
            payload["emoji"] = ":factory:"
        # Rocket.Chat rejects roomId/channel when the webhook's "Override Destination Channel" is off.
        # A channel without a recipient posts to the webhook's own channel, so a thread reply needs only tmid.
        if reply_to.get("room_id") and (claimed.get("recipient") or not reply_to.get("tmid")):
            payload["roomId"] = reply_to["room_id"]
        elif claimed.get("recipient") and not reply_to.get("room_id"):
            payload["channel"] = claimed["recipient"]
        if reply_to.get("tmid"):
            payload["tmid"] = reply_to["tmid"]
    try:
        response = client.post(url, json=payload, timeout=10, follow_redirects=False)
        # Never retain provider error text: it may echo tokens, URLs or message data.
        if response.status_code == 429:
            try:
                retry = int(response.headers.get("Retry-After", "0"))
                if claimed["provider"] == "telegram":
                    retry = max(retry, int(response.json().get("parameters", {}).get("retry_after", 0)))
            except (ValueError, TypeError, AttributeError):
                retry = 0
            return "Provider rate limit (HTTP 429)", min(max(retry, 0), 86400), False
        if not 200 <= response.status_code < 300:
            permanent = response.status_code < 500 and response.status_code not in {408, 425}
            return f"Provider rejected delivery (HTTP {response.status_code})", 0, permanent
        if claimed["provider"] == "telegram":
            try:
                if response.json().get("ok") is not True:
                    return "Telegram did not acknowledge delivery", 0, False
            except (ValueError, AttributeError):
                return "Invalid Telegram response", 0, False
        else:
            try:
                if response.json().get("success") is not True:
                    return "Rocket.Chat did not acknowledge delivery", 0, False
            except (ValueError, AttributeError):
                return "Invalid Rocket.Chat response", 0, False
        return None, 0, False
    except httpx.HTTPError:
        return "Notification transport error", 0, False


def _send_jira(client, config, reply_to, body):
    """Move a finished task to review first, then comment: a retry repeats only idempotent steps."""
    issue = reply_to.get("issue") or ""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,30}-[0-9]{1,9}", issue):
        return "Jira issue is missing", 0, True
    base = f"{config['base_url']}/rest/api/2/issue/{issue}"
    headers = {"Authorization": "Bearer " + config["token"], "Accept": "application/json"}

    def failed(response):
        if response.status_code == 429:
            return "Provider rate limit (HTTP 429)", 60, False
        if "json" not in response.headers.get("Content-Type", ""):
            # A gateway's HTML page (VPN down, reverse proxy blocking outside traffic), not Jira's answer.
            return f"{UNREACHABLE}Jira gateway answered HTTP {response.status_code}", 0, False
        permanent = response.status_code < 500 and response.status_code not in {408, 425}
        return f"Jira rejected delivery (HTTP {response.status_code})", 0, permanent

    def move(status, current):
        """Reach `status` directly or through the work status (workflows forbid "new" → "review")."""
        for _ in range(2):
            if not status or current == status:
                return None
            response = client.get(base + "/transitions", headers=headers, timeout=10)
            if response.status_code != 200:
                return failed(response)
            options = response.json().get("transitions", [])

            def leads(name):
                return next(
                    (t for t in options if name in {t.get("name"), (t.get("to") or {}).get("name")}), None
                )

            step = leads(status) or (
                leads(config["work_status"]) if current != config["work_status"] else None
            )
            # No path from here (already past it): the comment still goes.
            if not step:
                return None
            response = client.post(
                base + "/transitions", json={"transition": {"id": step["id"]}}, headers=headers, timeout=10
            )
            if not 200 <= response.status_code < 300:
                return failed(response)
            current = (step.get("to") or {}).get("name")
        return None

    try:
        kind = reply_to.get("kind")
        if reply_to.get("subtask"):
            kind = "progress"
        if (kind == "created" and (config["work_status"] or config["reviewer"])) or kind in JIRA_REVIEW_KINDS:
            people = ["assignee", *config["assignee_fields"]] if config["reviewer"] else []
            response = client.get(
                base, params={"fields": ",".join(["status", *people])}, headers=headers, timeout=10
            )
            if response.status_code != 200:
                return failed(response)
            fields = response.json().get("fields") or {}
            # The reviewer takes the issue first: workflows require an assignee to start or hand over.
            update = {
                field: {"name": config["reviewer"]}
                for field in people
                if (fields.get(field) or {}).get("name") != config["reviewer"]
            }
            if update:
                response = client.put(base, json={"fields": update}, headers=headers, timeout=10)
                if not 200 <= response.status_code < 300:
                    return failed(response)
            current = (fields.get("status") or {}).get("name")
            if kind == "created":
                # Only a new issue starts: one already in review or done keeps its status.
                category = ((fields.get("status") or {}).get("statusCategory") or {}).get("key")
                error = move(config["work_status"], current) if category == "new" else None
            else:
                error = move(config["review_status"], current)
            if error:
                return error
        response = client.post(base + "/comment", json={"body": body}, headers=headers, timeout=10)
        if not 200 <= response.status_code < 300:
            return failed(response)
        return None, 0, False
    except (httpx.HTTPError, ValueError, AttributeError):
        return f"{UNREACHABLE}Jira transport error", 0, False


def dispatch_notifications(*, limit=20, client=None):
    """Deliver due notifications. An injected httpx client enables offline tests."""
    if client is None:
        with httpx.Client(follow_redirects=False, trust_env=False) as http:
            return dispatch_notifications(limit=limit, client=http)
    processed = 0
    for _ in range(max(0, min(limit, 100))):
        claimed = _claim()
        if claimed is None:
            break
        processed += 1
        if claimed.get("skipped"):
            continue
        error, retry_after, permanent = _send(client, claimed)
        with transaction() as db:
            delivery = (
                db.query(NotificationDelivery)
                .filter_by(
                    id=claimed["id"],
                    state="sending",
                    lease_owner=claimed["owner"],
                )
                .first()
            )
            if delivery is None:
                continue
            delivery.lease_owner = None
            delivery.lease_until = None
            delivery.last_error = error
            if error is None:
                delivery.state = "sent"
                delivery.sent_at = now()
            elif error.startswith(UNREACHABLE) and now() - delivery.created < UNREACHABLE_DAYS * 86400:
                # An unreachable tracker is not a rejection: wait for the network without using up attempts.
                delivery.attempts = max(0, delivery.attempts - 1)
                delivery.state = "pending"
                delivery.retry_at = now() + UNREACHABLE_RETRY
            elif permanent or delivery.attempts >= MAX_ATTEMPTS:
                delivery.state = "failed"
            else:
                delivery.state = "pending"
                delivery.retry_at = now() + max(retry_after, min(30 * 2 ** (delivery.attempts - 1), 3600))
    return processed
