"""Terminal access to the same authenticated director dialogue used by the UI."""

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def response_json(response):
    if not response.is_success:
        try:
            detail = response.json().get("detail", f"HTTP {response.status_code}")
        except (ValueError, AttributeError):
            detail = f"HTTP {response.status_code}"
        raise RuntimeError(str(detail))
    return response.json()


def wait_reply(client, message_id, project_id, timeout, thread_id=None):
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            params = {"project_id": project_id} if project_id else {}
            if thread_id:
                params["thread_id"] = thread_id
            history = response_json(client.get("/api/config-chat", params=params))
            for message in history["messages"]:
                if message["id"] != message_id or message["role"] != "assistant":
                    continue
                if message["status"] in {"failed", "cancelled"}:
                    raise RuntimeError(message["content"])
                if message["status"] == "succeeded":
                    return message
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        print(
            f"Запрос {message_id} продолжает работу. Возобновить ожидание: --resume {message_id}"
            + (f" --thread {thread_id}" if thread_id else ""),
            file=sys.stderr,
        )
        raise
    raise TimeoutError(
        f"Ответ ещё не готов. Запрос {message_id} продолжает работу. "
        f"Возобновить ожидание: --resume {message_id} (с тем же --project, если он был указан)."
        + (f" Чат: --thread {thread_id}" if thread_id else "")
    )


def ask(client, message, project_id, timeout, thread_id=None):
    accepted = response_json(
        client.post(
            "/api/config-chat",
            json={
                "message": message,
                "project_id": project_id,
                **({"thread_id": thread_id} if thread_id else {}),
            },
        )
    )
    print(f"Запрос: {accepted['id']}", file=sys.stderr)
    return wait_reply(client, accepted["id"], project_id, timeout, thread_id)


def show(reply):
    print(reply["content"])
    if reply.get("organization_access_available"):
        print("Одноразовый доступ к новой организации забери в веб-интерфейсе.", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="darkfactory")
    commands = parser.add_subparsers(dest="command", required=True)
    director = commands.add_parser("director", help="Диалог с директором фабрики")
    director.add_argument("message", nargs="?", help="Без сообщения открывается интерактивный диалог")
    director.add_argument("--url", default=os.getenv("DARKFACTORY_URL", "http://127.0.0.1:8080"))
    director.add_argument("--token-file", type=Path, help="Файл API-токена; иначе DARKFACTORY_TOKEN")
    director.add_argument("--project", help="ID проекта; без него контекст всей фабрики")
    operation = director.add_mutually_exclusive_group()
    operation.add_argument("--resume", help="Ждать уже отправленный запрос, не создавая новый")
    operation.add_argument("--cancel", metavar="MESSAGE_ID", help="Отменить сообщение")
    operation.add_argument("--new-chat", action="store_true", help="Начать отдельный чат")
    director.add_argument("--thread", help="ID чата с директором")
    director.add_argument("--timeout", type=int, default=600, help="Время ожидания ответа в секундах")
    args = parser.parse_args(argv)
    if args.timeout < 1 or ((args.resume or args.cancel) and args.message is not None):
        parser.error(
            "--timeout должен быть положительным; --resume/--cancel нельзя сочетать с новым сообщением"
        )
    if args.new_chat and args.thread:
        parser.error("Выбери --new-chat или --thread")
    url = urlsplit(args.url)
    if (
        url.scheme not in {"http", "https"}
        or not url.netloc
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        parser.error("--url должен содержать HTTP(S) адрес фабрики без пути, логина и пароля")
    try:
        token = (
            args.token_file.read_text() if args.token_file else os.getenv("DARKFACTORY_TOKEN", "")
        ).strip()
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("Укажи API-токен через DARKFACTORY_TOKEN или --token-file")
        with httpx.Client(
            base_url=args.url.rstrip("/"),
            headers={"Authorization": "Bearer " + token},
            timeout=30,
            follow_redirects=False,
        ) as client:
            if args.cancel:
                result = response_json(client.post("/api/config-chat/messages/" + args.cancel + "/cancel"))
                print("Сообщение: " + result["status"])
                return 0
            if args.new_chat:
                thread = response_json(
                    client.post("/api/config-chat/threads", json={"project_id": args.project})
                )
                args.thread = thread["id"]
                print("Чат: " + args.thread, file=sys.stderr)
            if args.resume:
                show(wait_reply(client, args.resume, args.project, args.timeout, args.thread))
            elif args.message is not None:
                show(ask(client, args.message, args.project, args.timeout, args.thread))
            else:
                print("Директор фабрики. /exit — выход. Секреты добавляй через настройки.", file=sys.stderr)
                while True:
                    try:
                        message = input("Ты: ").strip()
                    except EOFError:
                        break
                    if message in {"/exit", "/quit"}:
                        break
                    if message:
                        show(ask(client, message, args.project, args.timeout, args.thread))
        return 0
    except (OSError, RuntimeError, ValueError, httpx.HTTPError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
