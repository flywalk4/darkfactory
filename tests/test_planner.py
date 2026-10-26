import copy
import io
import json

import pytest

from darkfactory import runner


def subtask(key, depends_on=None):
    return {
        "key": key,
        "title": f"Implement {key}",
        "request": f"Change only the {key} behavior.",
        "acceptance": f"The {key} regression check passes.",
        "depends_on": depends_on or [],
    }


def plan(subtasks=None):
    return {
        "plan": "Inspect, implement, and verify the requested behavior.",
        "questions": [],
        "subtasks": subtasks or [],
    }


def test_structured_plan_accepts_a_forward_referenced_dag():
    value = plan([subtask("integration", ["backend", "frontend"]), subtask("frontend"), subtask("backend")])
    assert runner.plan_object(json.dumps(value)) == value
    assert "subtasks" in runner.PLAN_SCHEMA["required"]
    assert runner.PLAN_SCHEMA["properties"]["subtasks"]["maxItems"] == 8


def test_legacy_running_plan_normalizes_to_no_children():
    value = {"plan": "Fix and verify existing behavior.", "questions": []}
    assert runner.plan_object(json.dumps(value)) == {**value, "subtasks": []}


def test_subtasks_cannot_decompose_when_the_job_forbids_it():
    with pytest.raises(ValueError, match="permitted decomposition"):
        runner.plan_object(json.dumps(plan([subtask("child")])), allow_subtasks=False)
    assert runner.plan_object(json.dumps(plan()), allow_subtasks=False)["subtasks"] == []


@pytest.mark.parametrize(
    "children",
    [
        [subtask("a"), subtask("a")],
        [subtask("a", ["unknown"])],
        [subtask("a", ["a"])],
        [subtask("a", ["b"]), subtask("b", ["c"]), subtask("c", ["a"])],
        [subtask("a", ["b", "b"]), subtask("b")],
        [subtask("bad key")],
        [subtask("a" * 41)],
        [subtask(str(index)) for index in range(9)],
        [{**subtask("a"), "title": " "}],
        [{**subtask("a"), "request": "x" * 20001}],
        [{**subtask("a"), "acceptance": "x" * 12001}],
        [{**subtask("a"), "depends_on": "b"}],
        [{**subtask("a"), "depends_on": [{}]}],
        [{**subtask("a"), "provider": "api"}],
        [{key: value for key, value in subtask("a").items() if key != "acceptance"}],
    ],
)
def test_invalid_or_unbounded_subtask_graph_is_rejected(children):
    with pytest.raises(ValueError):
        runner.plan_object(json.dumps(plan(children)))


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"plan": "", "questions": []},
        {"plan": "x" * 32001, "questions": []},
        {"plan": "Valid", "questions": [""]},
        {"plan": "Valid", "questions": ["x" * 2001]},
        {"plan": "Valid", "questions": ["why"] * 21},
        {"plan": "Valid", "questions": [], "subtasks": None},
        {**plan(), "approved": True},
    ],
)
def test_invalid_planner_envelope_is_rejected(value):
    with pytest.raises(ValueError):
        runner.plan_object(json.dumps(value))


@pytest.mark.parametrize("text", ["Explanation " + json.dumps(plan()), json.dumps(plan()) + " trailing"])
def test_planner_accepts_only_one_whole_json_response(text):
    with pytest.raises(ValueError):
        runner.plan_object(text)


def test_quota_uses_only_documented_known_window_values():
    assert runner.codex_quota({"primary": {"usedPercent": 30, "resetsAt": 2000000000}}) == {
        "remaining_percent": 70,
        "reset_at": 2000000000,
        "source": "codex.account/rateLimits/updated",
    }
    assert runner.codex_quota({"primary": {"usedPercent": 90}}) == {
        "remaining_percent": 10,
        "source": "codex.account/rateLimits/updated",
    }
    for snapshot in (None, {}, {"primary": {"resetsAt": 2000000000}}, {"primary": {"usedPercent": False}}):
        assert runner.codex_quota(snapshot) == {}


def test_exhausted_multiple_windows_wait_for_the_last_known_reset():
    assert (
        runner.codex_quota(
            {
                "primary": {"usedPercent": 100, "resetsAt": 2000000000},
                "secondary": {"usedPercent": 100, "resetsAt": 2000100000},
            }
        )["reset_at"]
        == 2000100000
    )
    quota = runner.codex_quota(
        {
            "primary": {"usedPercent": 50, "resetsAt": 2000100000},
            "secondary": {"usedPercent": 100, "resetsAt": 2000000000},
        }
    )
    assert quota["remaining_percent"] == 0 and quota["reset_at"] == 2000000000


class Process:
    def __init__(self, messages):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.mark.parametrize("outcome", ["success", "failed_turn", "failed_rpc"])
def test_codex_known_quota_survives_success_and_provider_errors(monkeypatch, outcome):
    messages = [
        {"id": 1, "result": {}},
        {"id": 3, "result": {"thread": {"id": "independent-plan-session"}}},
        {
            "method": "account/rateLimits/updated",
            "params": {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 100, "resetsAt": 2000000000, "windowDurationMins": 300},
                }
            },
        },
    ]
    if outcome == "failed_rpc":
        messages.append({"id": 4, "error": {"code": 429, "message": "rate_limit"}})
    else:
        messages.append({"id": 4, "result": {}})
        turn = (
            {"status": "completed", "items": []}
            if outcome == "success"
            else {"status": "failed", "error": {"message": "usage limit exceeded"}}
        )
        messages.append({"method": "turn/completed", "params": {"turn": turn}})
    proc = Process(copy.deepcopy(messages))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    result = runner.codex(
        {"credentials": {"accessToken": "synthetic-only"}, "role": "plan", "prompt": "Plan"}
    )
    assert result["quota"] == {
        "remaining_percent": 0,
        "reset_at": 2000000000,
        "source": "codex.account/rateLimits/updated",
    }
    assert result["session"] == "independent-plan-session"
    assert result["status"] == ("ok" if outcome == "success" else "quota")
    assert "account/login" not in proc.stdin.getvalue()


def test_codex_does_not_invent_absent_provider_quota(monkeypatch):
    proc = Process(
        [
            {"id": 1, "result": {}},
            {"id": 3, "result": {"thread": {"id": "independent-plan-session"}}},
            {"id": 4, "result": {}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed", "items": []}}},
        ]
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: proc)
    result = runner.codex(
        {"credentials": {"accessToken": "synthetic-only"}, "role": "plan", "prompt": "Plan"}
    )
    assert "quota" not in result
