"""Telegram bot: menus and buttons for the whole factory, over the normal API with the channel's command token.

Screens are edited in place. Free input (an answer, a new task, a message to the director) goes
through a force-reply prompt whose last line names what the reply is for, so no conversation state
is kept between updates. Subscription logins stay in the web settings: a chat never handles them.
"""

import html
import re
import time

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from .chat_commands import (
    COMMAND,
    TASK_ID,
    _access,
    _config,
    answer_task,
    record_sender,
    sender_allowed,
    submit_command,
)
from .db import Session, transaction
from .director import call
from .notifications import NotificationChannel

POLL_SECONDS = 25
# Telegram sends a forward's comment as a separate message just before the forward itself.
HOLD_SECONDS = 2
READY = {"queued", "waiting_pool", "waiting_dependency", "waiting_integration"}
COLUMNS = [
    ("⏳", "К запуску", READY),
    ("⚙️", "В работе", {"running", "recovering"}),
    ("🙋", "Нужно внимание", {"needs_human", "review_required"}),
    ("✅", "Завершены", {"merged", "cancelled", "duplicate", "checked"}),
]
STATES = {
    "queued": "В очереди",
    "waiting_pool": "Ждёт подписку",
    "waiting_dependency": "Ждёт зависимость",
    "waiting_integration": "Ждёт интеграцию",
    "running": "В работе",
    "recovering": "Восстанавливается",
    "needs_human": "Нужно решение",
    "review_required": "Ждёт слияния человеком",
    "merged": "Принята",
    "cancelled": "Отменена",
    "checked": "Проверка пройдена",
    "duplicate": "Дубликат",
}
PRIORITIES = ["low", "normal", "high", "critical"]
PRIORITY_LABELS = {"critical": "🔥 критичный", "high": "⏫ высокий", "normal": "обычный", "low": "⏬ низкий"}
QUESTION_KINDS = {
    "clarification",
    "limit",
    "needs_human",
    "execution_error",
    "network_blocked",
    "environment_blocked",
    "review_repeated",
    "stopped",
    "review_required",
    "supervisor_escalated",
}
PAGE = 8
# Last line of a prompt: what a reply to it is for.
PROMPT = re.compile(r"(NewTask|Director)(?: ([0-9a-f]{32}))?\s*$")
MENU = re.compile(r"^/(start|menu)(?:@\w+)?$|^меню$", re.I)


def esc(value):
    return html.escape(str(value or ""))


class Bot:
    def __init__(self, client, token, channel_id, config):
        self.client, self.token, self.channel_id, self.config = client, token, channel_id, config
        self.commands = config.get("director_token")
        self.chat = config["chat_id"]
        self.sender = None

    def telegram(self, method, **payload):
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=10
            )
            value = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return value.get("result") if isinstance(value, dict) and value.get("ok") else None

    def factory(self, method, path, body=None):
        return call(self.commands, method, path, body)

    def send(self, text, rows=None, reply_to=None, prompt=None):
        payload = {
            "chat_id": self.chat,
            "text": text[:4000],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if rows:
            payload["reply_markup"] = keyboard(rows)
        if prompt:
            payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": prompt[:64]}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.telegram("sendMessage", **payload)

    def show(self, screen, message_id=None):
        text, rows = screen
        if message_id is None:
            return self.send(text, rows)
        edited = self.telegram(
            "editMessageText",
            chat_id=self.chat,
            message_id=message_id,
            text=text[:4000],
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup=keyboard(rows),
        )
        return edited


def keyboard(rows):
    return {
        "inline_keyboard": [
            [
                {"text": label, "url": target[4:]}
                if target.startswith("url:")
                else {"text": label, "callback_data": target}
                for label, target in row
            ]
            for row in rows
            if row
        ]
    }


def detail(value):
    found = value.get("detail") if isinstance(value, dict) else None
    return found if isinstance(found, str) else "операция не выполнена"


def column_of(state):
    return next((index for index, (_, _, states) in enumerate(COLUMNS) if state in states), 3)


def queue_order(task):
    return (-PRIORITIES.index(task.get("priority", "normal")), task.get("created") or 0)


# Screens: each returns (HTML text, rows of (label, callback data or "url:...")).


def home(bot):
    code, projects = bot.factory("GET", "/api/projects")
    if code != 200:
        return "Нет доступа к фабрике: " + esc(detail(projects)), [[("🔄 Повторить", "h")]]
    rows = [[("📁 " + project["name"][:40], "p:" + project["id"])] for project in projects[:20]]
    rows.append([("🙋 Ждут ответа", "w"), ("🧠 Директор", "d")])
    text = (
        "<b>Darkfactory</b>\nВыбери проект или раздел."
        if projects
        else "<b>Darkfactory</b>\nПроектов пока нет."
    )
    return text, rows


def tasks(bot, project_id):
    code, value = bot.factory("GET", f"/api/projects/{project_id}/tasks")
    return value if code == 200 and isinstance(value, list) else []


def project_name(bot, project_id):
    code, projects = bot.factory("GET", "/api/projects")
    if code == 200:
        for project in projects:
            if project["id"] == project_id:
                return project["name"]
    return "Проект"


def board(bot, project_id):
    items = tasks(bot, project_id)
    lines = [f"📁 <b>{esc(project_name(bot, project_id))}</b>", ""]
    rows = []
    for index, (icon, title, states) in enumerate(COLUMNS):
        count = sum(task["state"] in states for task in items)
        lines.append(f"{icon} {title}: <b>{count}</b>")
        rows.append([(f"{icon} {title} · {count}", f"c:{project_id}:{index}:0")])
    rows.append([("➕ Новая задача", "n:" + project_id), ("🧠 Директор", "d:" + project_id)])
    rows.append([("🔄 Обновить", "p:" + project_id), ("« Проекты", "h")])
    return "\n".join(lines), rows


def task_label(task):
    mark = {"critical": "🔥 ", "high": "⏫ ", "low": "⏬ "}.get(task.get("priority"), "")
    return mark + task["title"][:50]


def column(bot, project_id, index, page):
    icon, title, states = COLUMNS[index]
    items = [task for task in tasks(bot, project_id) if task["state"] in states]
    if index == 0:
        items.sort(key=queue_order)
    shown = items[page * PAGE : (page + 1) * PAGE]
    lines = [f"{icon} <b>{title}</b> · {len(items)}"]
    if index == 0 and items:
        lines.append("Сверху — первые в очереди. Порядок меняется приоритетом в карточке.")
    if not items:
        lines.append("Пусто.")
    rows = [[(task_label(task), "t:" + task["id"])] for task in shown]
    pages = []
    if page:
        pages.append(("‹ Назад", f"c:{project_id}:{index}:{page - 1}"))
    if (page + 1) * PAGE < len(items):
        pages.append(("Дальше ›", f"c:{project_id}:{index}:{page + 1}"))
    rows.append(pages)
    rows.append([("« Доска", "p:" + project_id)])
    return "\n".join(lines), rows


def waiting(bot):
    code, projects = bot.factory("GET", "/api/projects")
    items = []
    for project in projects if code == 200 else []:
        items += [
            task for task in tasks(bot, project["id"]) if task["state"] in {"needs_human", "review_required"}
        ]
    rows = [[(task_label(task), "t:" + task["id"])] for task in items[:15]]
    rows.append([("« Меню", "h")])
    text = f"🙋 <b>Ждут ответа</b> · {len(items)}" if items else "🙋 Никто не ждёт ответа."
    return text, rows


def card(bot, task_id):
    code, task = bot.factory("GET", f"/api/tasks/{task_id}")
    if code != 200:
        return "Задача недоступна: " + esc(detail(task)), [[("« Меню", "h")]]
    state = task["state"]
    lines = [
        f"<b>{esc(task['title'])}</b>",
        f"{COLUMNS[column_of(state)][0]} {STATES.get(state, state)} · приоритет {PRIORITY_LABELS.get(task.get('priority'), task.get('priority'))}",
    ]
    if task.get("reason"):
        lines.append(f"Причина: {esc(task['reason'])}")
    if state in {"needs_human", "review_required"}:
        question = next(
            (event for event in reversed(task.get("events") or []) if event.get("kind") in QUESTION_KINDS),
            None,
        )
        if question:
            lines += ["", "<b>Вопрос:</b>", esc(str(question.get("message"))[:1500])]
    if task.get("commit_url"):
        lines.append(f'\n<a href="{esc(task["commit_url"])}">Коммит</a>')
    lines += ["", f"<code>Task {task_id}</code>"]
    rows = []
    if state == "needs_human":
        rows.append([("✍️ Ответить", "a:" + task_id), ("▶️ Продолжить", "r:" + task_id)])
    if state == "review_required":
        rows.append([("✅ Слить", "m:" + task_id), ("✍️ Доработать", "a:" + task_id)])
    if state in READY:
        rows.append([("⏫ Выше в очереди", "u:" + task_id), ("⏬ Ниже", "dn:" + task_id)])
    rows.append(
        [("➡️ Переместить", "mv:" + task_id)]
        + ([("🗑 Отменить", "x:" + task_id)] if state not in {"merged", "cancelled", "duplicate"} else [])
    )
    rows.append(
        [("🔄 Обновить", "t:" + task_id), ("« Колонка", f"c:{task['project_id']}:{column_of(state)}:0")]
    )
    return "\n".join(lines), rows


def move(bot, task_id):
    code, task = bot.factory("GET", f"/api/tasks/{task_id}")
    if code != 200:
        return "Задача недоступна.", [[("« Меню", "h")]]
    state = task["state"]
    rows = []
    if state in READY:
        rows.append([("🔝 В начало очереди", f"pr:{task_id}:critical"), ("🔚 В конец", f"pr:{task_id}:low")])
    elif state not in {"running", "recovering", "merged", "duplicate"}:
        rows.append([("⏳ К запуску", "r:" + task_id)])
    if state == "review_required":
        rows.append([("✅ В завершённые (слить)", "m:" + task_id)])
    if state not in {"merged", "cancelled", "duplicate"}:
        rows.append([("🗑 Отменить", "x:" + task_id)])
    rows.append([("« Карточка", "t:" + task_id)])
    current = COLUMNS[column_of(state)]
    return f"➡️ Куда переместить «{esc(task['title'])}»?\nСейчас: {current[0]} {current[1]}", rows


def prompt(bot, text, marker, placeholder):
    bot.send(f"{text}\n\n<code>{marker}</code>", prompt=placeholder)


def on_callback(bot, query):
    data = query.get("data") or ""
    message = query.get("message") or {}
    message_id = message.get("message_id")
    if (message.get("text") or "").startswith("Darkfactory ·"):
        # Keep the notification: open screens from it as new messages.
        message_id = None
    toast = None
    kind, _, rest = data.partition(":")
    screen = None
    if kind == "h":
        screen = home(bot)
    elif kind == "w":
        screen = waiting(bot)
    elif kind == "p":
        screen = board(bot, rest)
    elif kind == "c":
        project_id, index, page = rest.split(":")
        screen = column(bot, project_id, int(index), int(page))
    elif kind == "t":
        screen = card(bot, rest)
    elif kind == "mv":
        screen = move(bot, rest)
    elif kind in {"r", "xx", "m"}:
        action = {"r": "resume", "xx": "cancel", "m": "merge"}[kind]
        code, value = bot.factory("POST", f"/api/tasks/{rest}/actions", {"action": action})
        toast = {"resume": "Задача в очереди", "cancel": "Задача отменена", "merge": "Слияние запущено"}[
            action
        ]
        toast = toast if code == 200 else detail(value)
        if code == 200:
            record_sender(rest, "telegram", bot.sender)
        screen = card(bot, rest)
    elif kind == "x":
        screen = (
            "🗑 Отменить задачу? Начатая работа остановится.",
            [[("Да, отменить", "xx:" + rest), ("Нет", "t:" + rest)]],
        )
    elif kind in {"u", "dn", "pr"}:
        task_id, _, target = rest.partition(":")
        if kind != "pr":
            _, task = bot.factory("GET", f"/api/tasks/{task_id}")
            current = PRIORITIES.index(task.get("priority", "normal")) if isinstance(task, dict) else 1
            target = PRIORITIES[max(0, min(3, current + (1 if kind == "u" else -1)))]
        code, value = bot.factory("PUT", f"/api/tasks/{task_id}/priority", {"priority": target})
        toast = "Приоритет: " + PRIORITY_LABELS[target] if code == 200 else detail(value)
        if code == 200:
            record_sender(task_id, "telegram", bot.sender)
        screen = card(bot, task_id)
    elif kind == "a":
        _, task = bot.factory("GET", f"/api/tasks/{rest}")
        title = task.get("title", "") if isinstance(task, dict) else ""
        prompt(
            bot,
            f"✍️ Ответ агенту по задаче «{esc(title)}». Ответь на это сообщение.",
            f"Task {rest}",
            "Ответ агенту",
        )
    elif kind == "n":
        prompt(
            bot,
            f"➕ Новая задача в «{esc(project_name(bot, rest))}». Ответь на это сообщение: первая строка — название, дальше описание.",
            f"NewTask {rest}",
            "Название и описание",
        )
    elif kind == "d":
        marker = f"Director {rest}" if rest else "Director"
        prompt(bot, "🧠 Что передать директору? Ответь на это сообщение.", marker, "Сообщение директору")
    bot.telegram("answerCallbackQuery", callback_query_id=query.get("id"), text=(toast or "")[:190])
    if screen:
        bot.show(screen, message_id)


def on_reply(bot, message, quoted, text):
    """A reply to one of our prompts. Returns False when the quoted message is not a prompt."""
    lines = (quoted.get("text") or "").strip().splitlines()
    found = PROMPT.search(lines[-1]) if lines else None
    if not found:
        return False
    kind, target = found.groups()
    message_id = message["message_id"]
    if kind == "NewTask" and target:
        title, _, rest = text.strip().partition("\n")
        body = {"title": title.strip()[:300] or "Задача", "request": text.strip()}
        code, task = bot.factory("POST", f"/api/projects/{target}/tasks", body)
        if code == 201:
            record_sender(task["id"], "telegram", bot.sender)
            bot.show(card(bot, task["id"]))
        else:
            bot.send("Задача не создана: " + esc(detail(task)), reply_to=message_id)
    elif kind == "Director":
        submit_command(
            bot.channel_id,
            text,
            f"{bot.chat}:{message_id}",
            {"message_id": message_id},
            project_id=target,
            sender=bot.sender,
        )
    return True


def on_message(bot, message, comment=None):
    sender, chat = message.get("from") or {}, message.get("chat") or {}
    if sender.get("is_bot") or not isinstance(message.get("message_id"), int):
        return
    text = (message.get("text") or message.get("caption") or "").strip()
    message_id = message["message_id"]
    ref = f"{chat.get('id')}:{message_id}"
    reply_to = {"message_id": message_id}
    quoted = message.get("reply_to_message") or {}
    if MENU.match(text):
        bot.show(home(bot))
    elif command := COMMAND.match(text):
        submit_command(bot.channel_id, command.group(1), ref, reply_to, sender=bot.sender)
    elif (quoted.get("from") or {}).get("is_bot") and text and on_reply(bot, message, quoted, text):
        pass
    elif (
        (quoted.get("from") or {}).get("is_bot")
        and text
        and (quoted.get("text") or "").startswith("Директор:")
    ):
        submit_command(bot.channel_id, text, ref, reply_to, sender=bot.sender)
    elif (quoted.get("from") or {}).get("is_bot") and (task := TASK_ID.search(quoted.get("text") or "")):
        answer_task(bot.channel_id, task.group(1), text, ref, reply_to, sender=bot.sender)
    elif message.get("forward_origin") or message.get("forward_date"):
        task = TASK_ID.search(text)
        if task and comment:
            answer_task(bot.channel_id, task.group(1), comment[0], comment[1], reply_to, sender=bot.sender)
        elif task:
            prompt(
                bot,
                "✍️ Что ответить агенту по этой задаче? Ответь на это сообщение.",
                f"Task {task.group(1)}",
                "Ответ агенту",
            )
    elif text and chat.get("type") == "private":
        # In a private chat with the bot, plain text is a message to the director.
        submit_command(bot.channel_id, text, ref, reply_to, sender=bot.sender)


def is_comment(message):
    return (
        (message.get("chat") or {}).get("type") == "private"
        and bool(message.get("text"))
        and not message.get("reply_to_message")
        and not (message.get("forward_origin") or message.get("forward_date"))
        and not COMMAND.match(message["text"].strip())
        and not MENU.match(message["text"].strip())
    )


def is_forward(message):
    return bool(message.get("forward_origin") or message.get("forward_date"))


def channels():
    """Enabled Telegram channels with a command token, grouped by bot token."""
    bots = {}
    with Session() as db:
        for channel in db.scalars(
            select(NotificationChannel).where(
                NotificationChannel.provider == "telegram", NotificationChannel.enabled.is_(True)
            )
        ):
            config = _config(channel)
            if not config.get("director_token"):
                continue
            try:
                # Menus call the API with this token directly: an owner's or a revoked one serves nobody.
                _access(db, channel, config["director_token"])
            except HTTPException:
                continue
            else:
                bots.setdefault(config["bot_token"], []).append((channel.id, config, channel.command_offset))
    return bots


def chat_matches(config, chat):
    configured = config.get("chat_id", "")
    return str(chat.get("id")) == configured or (
        configured.startswith("@") and chat.get("username") == configured[1:]
    )


def poll(client, wait=0):
    """Read and handle one batch per bot; returns how many bots are configured."""
    bots = channels()
    for token, items in bots.items():
        offsets = [offset for _, _, offset in items if offset is not None]
        params = {"timeout": wait if len(bots) == 1 else 0, "allowed_updates": '["message","callback_query"]'}
        if offsets:
            params["offset"] = max(offsets)
        try:
            response = client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=params["timeout"] + 10,
            )
            updates = response.json().get("result") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError):
            continue
        if not isinstance(updates, list) or not updates:
            continue
        updates = [update for update in updates if isinstance(update, dict) and "update_id" in update]
        handled = None
        for index, update in enumerate(updates):
            message = update.get("message")
            query = update.get("callback_query")
            chat = (message or (query or {}).get("message") or {}).get("chat") or {}
            target = next(((cid, config) for cid, config, _ in items if chat_matches(config, chat)), None)
            following = updates[index + 1].get("message") if index + 1 < len(updates) else None
            if target and isinstance(message, dict) and is_comment(message):
                if following is None and time.time() - message.get("date", 0) < HOLD_SECONDS:
                    break  # Its forward may still be on the way: read it again next time.
                if isinstance(following, dict) and is_forward(following):
                    handled = update["update_id"]
                    continue  # Becomes the forward's answer below.
            sender = ((query if isinstance(query, dict) else message) or {}).get("from") or {}
            if target and not sender_allowed(target[1], sender.get("id")):
                # Only the operator-listed people of this chat may act through the bot.
                target = None
            if target:
                bot = Bot(client, token, *target)
                bot.sender = sender.get("id")
                if isinstance(query, dict):
                    on_callback(bot, query)
                elif isinstance(message, dict):
                    previous = updates[index - 1].get("message") if index else None
                    comment = None
                    if is_forward(message) and isinstance(previous, dict) and is_comment(previous):
                        comment = (previous["text"], f"{chat.get('id')}:{previous['message_id']}")
                    on_message(bot, message, comment)
            handled = update["update_id"]
        if handled is None:
            continue
        with transaction() as db:
            for channel_id, _, _ in items:
                db.get(NotificationChannel, channel_id).command_offset = handled + 1
    return len(bots)


def listen():
    """Worker loop body: long-poll Telegram so buttons answer at once."""
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        if not poll(client, POLL_SECONDS):
            time.sleep(5)
