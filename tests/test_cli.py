import json

import httpx
import pytest

from darkfactory import cli


def test_terminal_dialogue_uses_authenticated_api_and_project(monkeypatch, capsys, tmp_path):
    requests = []
    history = [
        {"messages": [{"id": "message1", "role": "assistant", "status": "running"}]},
        {
            "messages": [
                {"id": "another", "role": "assistant", "status": "succeeded", "content": "Other context"},
                {"id": "message1", "role": "assistant", "status": "succeeded", "content": "Пул создан"},
            ]
        },
    ]

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer private-token"
        if request.method == "POST":
            assert json.loads(request.content) == {"message": "Создай пул", "project_id": "project1"}
            return httpx.Response(202, json={"id": "message1", "status": "queued"})
        assert request.url.params["project_id"] == "project1"
        return httpx.Response(200, json=history.pop(0))

    real_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    token_file = tmp_path / "token"
    token_file.write_text("private-token\n")
    assert cli.main(["director", "--token-file", str(token_file), "--project", "project1", "Создай пул"]) == 0
    assert len(requests) == 3
    output = capsys.readouterr()
    assert output.out == "Пул создан\n"
    assert "private-token" not in output.out + output.err


def test_resume_never_submits_action_twice(monkeypatch, capsys):
    def handle(request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "messages": [{"id": "known", "role": "assistant", "status": "succeeded", "content": "Готово"}]
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setenv("DARKFACTORY_TOKEN", "private-token")
    assert cli.main(["director", "--resume", "known"]) == 0
    assert capsys.readouterr().out == "Готово\n"


def test_wait_timeout_preserves_request_and_resume_command(monkeypatch):
    times = iter([0, 0, 2, 2])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    with httpx.Client(
        base_url="http://example.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"messages": []})),
    ) as client:
        with pytest.raises(TimeoutError, match="--resume known"):
            cli.wait_reply(client, "known", None, 1)


@pytest.mark.parametrize("status", [401, 403, 409])
def test_api_refusal_is_reported_without_retry(status):
    with httpx.Client(
        base_url="http://example.invalid",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"detail": "Доступ отклонён"})),
    ) as client:
        with pytest.raises(RuntimeError, match="Доступ отклонён"):
            cli.ask(client, "Создай проект", None, 1)


def test_missing_token_requires_local_configuration(monkeypatch, capsys):
    monkeypatch.delenv("DARKFACTORY_TOKEN", raising=False)
    assert cli.main(["director", "Покажи проекты"]) == 1
    assert "DARKFACTORY_TOKEN" in capsys.readouterr().err


def test_new_terminal_chat_and_resume_are_scoped(monkeypatch, capsys):
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/config-chat/threads":
            return httpx.Response(201, json={"id": "thread1"})
        if request.method == "POST":
            assert json.loads(request.content)["thread_id"] == "thread1"
            return httpx.Response(202, json={"id": "message1"})
        assert request.url.params["thread_id"] == "thread1"
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "id": "message1",
                        "role": "assistant",
                        "status": "succeeded",
                        "content": "Ответ в новом чате",
                    }
                ]
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setenv("DARKFACTORY_TOKEN", "private-token")
    assert cli.main(["director", "--new-chat", "Привет"]) == 0
    assert capsys.readouterr().out == "Ответ в новом чате\n"
    assert seen == [
        ("POST", "/api/config-chat/threads"),
        ("POST", "/api/config-chat"),
        ("GET", "/api/config-chat"),
    ]
    assert cli.main(["director", "--thread", "thread1", "--resume", "message1"]) == 0


def test_terminal_cancel_never_submits_new_message(monkeypatch, capsys):
    def handle(request):
        assert request.method == "POST" and request.url.path == "/api/config-chat/messages/known/cancel"
        return httpx.Response(200, json={"id": "known", "status": "cancelling"})

    real_client = httpx.Client
    monkeypatch.setattr(
        cli.httpx, "Client", lambda **kw: real_client(**kw, transport=httpx.MockTransport(handle))
    )
    monkeypatch.setenv("DARKFACTORY_TOKEN", "private-token")
    assert cli.main(["director", "--cancel", "known"]) == 0
    assert "cancelling" in capsys.readouterr().out


def test_cancelled_reply_stops_terminal_wait_immediately():
    with httpx.Client(
        base_url="http://example.invalid",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "known", "role": "assistant", "status": "cancelled", "content": "Отменено"}
                    ]
                },
            )
        ),
    ) as client:
        with pytest.raises(RuntimeError, match="Отменено"):
            cli.wait_reply(client, "known", None, 600)
