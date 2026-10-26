"""Token and time metrics over recent factory tasks: the factory page and the CLI.

Runs inside the worker container:

    docker compose exec -T worker python -m darkfactory.metrics --last 100 [--since ISO] [--json]

Reads tasks and events from the database and agent runs from
`<DATA_ROOT>/artifacts/<task>/browser/<generation>/` (execution.json, usage.json, transcript.jsonl).
Everything below `load_tasks` is pure and tested in tests/test_factory_metrics.py.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROLES = ("plan", "implement", "review", "director")
HUMAN_EVENTS = {"stopped", "clarification", "limit"}
RESUME_MARK = "Продолжаю прежнюю сессию"
SETUP_SKIPPED = "setup пропущен"
SHELLS = {"sh", "bash", "/bin/sh", "/bin/bash", "/usr/bin/bash", "zsh"}
SEPARATORS = {"&&", "||", ";", "|"}
ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def summary(values: list[float]) -> dict:
    values = [value for value in values if value is not None]
    if not values:
        return {"n": 0, "sum": 0, "avg": None, "median": None, "max": None}
    return {
        "n": len(values),
        "sum": sum(values),
        "avg": sum(values) / len(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def phase_durations(events: list[tuple[float, str, str]]) -> list[tuple[str, float]]:
    """Pair each phase_started with the next phase_finished of the same phase; seconds per pair."""
    opened, pairs = {}, []
    for at, kind, message in sorted(events):
        if kind == "phase_started":
            opened[message] = at
        elif kind == "phase_finished" and message in opened:
            pairs.append((message, at - opened.pop(message)))
    return pairs


def split_words(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def command_key(text: str) -> str:
    """Short key for a transcript tool entry: the first two words of the real command."""
    text = text.strip()
    if not text.startswith("$ "):
        name, _, rest = text.partition(" ")
        if name == "Bash":
            try:
                text = "$ " + json.loads(rest)["command"]
            except (ValueError, KeyError, TypeError):
                return name
        else:
            return name or "?"
    command = text[2:].strip()
    for _ in range(3):
        words = split_words(command)
        if len(words) >= 3 and words[0] in SHELLS and words[1] in {"-c", "-lc", "-ec"}:
            command = words[2]
            continue
        break
    if "<<" in command:
        # Heredoc bodies are data, not commands: keep only the line that starts them.
        words = split_words(command.split("\n", 1)[0])
    segments, current = [], []
    for word in words:
        if word in SEPARATORS:
            segments.append(current)
            current = []
        elif word.endswith(";"):
            current.append(word[:-1])
            segments.append(current)
            current = []
        else:
            current.append(word)
    segments.append(current)
    for segment in segments:
        segment = [word.lstrip("(") for word in segment]
        while segment and ENV.match(segment[0]):
            segment = segment[1:]
        segment = [word for word in segment if word and word[0] not in "<>"]
        if segment and segment[0] != "cd":
            return " ".join(segment[:2])
    return "?"


def command_times(entries: list[dict]) -> list[tuple[str, float]]:
    """(key, seconds) for every tool entry answered by an output before the next tool entry."""
    pending, result = None, []
    for entry in entries:
        kind = entry.get("kind")
        if kind == "tool":
            pending = entry
        elif kind == "output" and pending is not None:
            result.append((command_key(pending.get("text", "")), max(0.0, entry["at"] - pending["at"])))
            pending = None
    return result


def run_profile(entries: list[dict]) -> dict:
    """Tool calls, setup seconds before the agent first speaks, and whether setup was skipped."""
    start = entries[0]["at"] if entries else None
    first = next((entry["at"] for entry in entries if entry.get("kind") in {"message", "reasoning"}), None)
    return {
        "start": start,
        "tool_calls": sum(entry.get("kind") == "tool" for entry in entries),
        "setup_seconds": first - start if first is not None and start is not None else None,
        "setup_skipped": any(
            entry.get("kind") == "status" and SETUP_SKIPPED in entry.get("text", "") for entry in entries
        ),
        "resumed": any(
            RESUME_MARK in entry.get("text", "") for entry in entries if entry.get("kind") == "message"
        ),
        "agent": next(
            (
                entry["text"].rsplit("·", 1)[1].strip()
                for entry in entries
                if entry.get("kind") == "status"
                and entry.get("text", "").startswith("Запуск:")
                and "·" in entry["text"]
            ),
            None,
        ),
    }


def adjust_resumed(runs: list[dict]) -> list[dict]:
    """Codex usage is cumulative per thread: a resumed run subtracts the previous run of its role."""
    previous = {}
    for run in sorted(runs, key=lambda item: item.get("start") or 0):
        usage = run.get("usage")
        if not usage:
            continue
        key = (run.get("role"), run.get("agent"))
        raw = {
            name: usage.get(name, 0) or 0 for name in ("input_tokens", "cached_input_tokens", "output_tokens")
        }
        run["tokens"] = dict(raw)
        if run.get("resumed") and run.get("agent") == "codex" and key in previous:
            run["tokens"] = {name: max(0, raw[name] - previous[key][name]) for name in raw}
            run["adjusted"] = True
        previous[key] = raw
    return runs


def task_metrics(task: dict) -> dict:
    events = task["events"]
    kinds = [kind for _, kind, _ in events]
    phases = phase_durations(events)
    times = [at for at, _, _ in events]
    return {
        "merged": task["state"] == "merged",
        "busy_seconds": sum(seconds for _, seconds in phases),
        "busy_no_monitor_seconds": sum(seconds for phase, seconds in phases if phase != "monitor"),
        "wall_seconds": max(times) - min(times) if times else 0,
        "phases": phases,
        "checks_runs": sum(
            1 for at, kind, message in events if kind == "phase_started" and message == "checks"
        ),
        "corrections": task.get("corrections", 0) or 0,
        "correction_events": kinds.count("correction"),
        "human": any(kind in HUMAN_EVENTS for kind in kinds),
        "runs": adjust_resumed(task["runs"]),
    }


def group_report(tasks: list[dict]) -> dict:
    metrics = [task_metrics(task) for task in tasks]
    runs = [run for item in metrics for run in item["runs"]]
    count = len(metrics)
    roles = {}
    for role in sorted({run.get("role") or "?" for run in runs}, key=lambda name: (name not in ROLES, name)):
        mine = [run for run in runs if (run.get("role") or "?") == role and "tokens" in run]
        inputs = [run["tokens"]["input_tokens"] for run in mine]
        cached = sum(run["tokens"]["cached_input_tokens"] for run in mine)
        roles[role] = {
            "runs": len([run for run in runs if (run.get("role") or "?") == role]),
            "with_usage": len(mine),
            "input": summary(inputs),
            "output": sum(run["tokens"]["output_tokens"] for run in mine),
            "cached_pct": 100 * cached / sum(inputs) if sum(inputs) else None,
            "adjusted": sum(1 for run in mine if run.get("adjusted")),
        }
    phases = {}
    for item in metrics:
        for phase, seconds in item["phases"]:
            phases.setdefault(phase, []).append(seconds / 60)
    busy = sum(item["busy_seconds"] for item in metrics) / 60
    wall = sum(item["wall_seconds"] for item in metrics) / 60
    commands = {}
    for run in runs:
        for key, seconds in command_times(run.get("entries", [])):
            bucket = commands.setdefault(run.get("role") or "?", {}).setdefault(key, [0, 0.0])
            bucket[0] += 1
            bucket[1] += seconds
    implement = [run for run in runs if run.get("role") == "implement"]
    per_tool = [
        run["tokens"]["input_tokens"] / run["tool_calls"]
        for run in implement
        if "tokens" in run and run["tool_calls"]
    ]
    total_input = sum(run["tokens"]["input_tokens"] for run in runs if "tokens" in run)
    profiles = {}
    for run in runs:
        if "tokens" in run:
            row = profiles.setdefault(run.get("profile") or "—", {"runs": 0, "input": 0, "output": 0})
            row["runs"] += 1
            row["input"] += run["tokens"]["input_tokens"]
            row["output"] += run["tokens"]["output_tokens"]
    return {
        "tasks": count,
        "task_minutes": summary([item["busy_no_monitor_seconds"] / 60 for item in metrics]),
        "profiles": profiles,
        "input_per_task": total_input / count if count else None,
        "roles": roles,
        "busy_minutes": busy,
        "busy_no_monitor_minutes": sum(item["busy_no_monitor_seconds"] for item in metrics) / 60,
        "wall_minutes": wall,
        "busy_ratio": busy / wall if wall else None,
        "phases": {phase: summary(values) for phase, values in sorted(phases.items())},
        "checks_per_task": sum(item["checks_runs"] for item in metrics) / count if count else None,
        "corrections_per_task": sum(item["corrections"] for item in metrics) / count if count else None,
        "correction_events_per_task": sum(item["correction_events"] for item in metrics) / count
        if count
        else None,
        "no_human_share": sum(not item["human"] for item in metrics) / count if count else None,
        "commands": {
            role: sorted(
                (
                    {"command": key, "calls": calls, "minutes": seconds / 60}
                    for key, (calls, seconds) in found.items()
                ),
                key=lambda row: -row["minutes"],
            )[:15]
            for role, found in sorted(commands.items())
        },
        "implement": {
            "runs": len(implement),
            "tool_calls_median": summary([run["tool_calls"] for run in implement])["median"],
            "tokens_per_tool_call_median": summary(per_tool)["median"],
            "setup_minutes": summary(
                [run["setup_seconds"] / 60 for run in implement if run.get("setup_seconds") is not None]
            ),
            "setup_skipped_share": (
                sum(run["setup_skipped"] for run in implement) / len(implement) if implement else None
            ),
        },
    }


def report(tasks: list[dict]) -> dict:
    return {
        "merged": group_report([task for task in tasks if task["state"] == "merged"]),
        "all": group_report(tasks),
    }


def number(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and abs(value) >= 1000 or isinstance(value, int):
        return f"{round(value):,}".replace(",", " ")
    return f"{value:.{digits}f}"


def render(data: dict) -> str:
    lines = []
    for name in ("merged", "all"):
        group = data[name]
        lines += [
            f"== {name}: {group['tasks']} tasks",
            f"input tokens per task: {number(group['input_per_task'])}",
            f"busy {number(group['busy_minutes'])} min (without monitor {number(group['busy_no_monitor_minutes'])})"
            f" / wall {number(group['wall_minutes'])} min, ratio {number(group['busy_ratio'], 2)}",
            f"checks per task {number(group['checks_per_task'], 2)}, corrections per task "
            f"{number(group['corrections_per_task'], 2)} (events {number(group['correction_events_per_task'], 2)}), "
            f"without needs_human {number(100 * group['no_human_share'] if group['no_human_share'] is not None else None)}%",
            "",
            f"{'role':<12}{'runs':>6}{'usage':>7}{'sum in':>14}{'median':>12}{'max':>12}{'cached%':>9}{'resumed':>9}",
        ]
        for role, row in group["roles"].items():
            lines.append(
                f"{role:<12}{row['runs']:>6}{row['with_usage']:>7}{number(row['input']['sum']):>14}"
                f"{number(row['input']['median']):>12}{number(row['input']['max']):>12}"
                f"{number(row['cached_pct']):>9}{row['adjusted']:>9}"
            )
        lines += ["", f"{'phase (min)':<12}{'count':>6}{'avg':>9}{'median':>9}{'max':>9}"]
        for phase, row in group["phases"].items():
            lines.append(
                f"{phase:<12}{row['n']:>6}{number(row['avg']):>9}{number(row['median']):>9}{number(row['max']):>9}"
            )
        impl = group["implement"]
        lines += [
            "",
            f"implement: {impl['runs']} runs, median tool calls {number(impl['tool_calls_median'])}, "
            f"median tokens per tool call {number(impl['tokens_per_tool_call_median'])}, "
            f"setup median {number(impl['setup_minutes']['median'])} min / max {number(impl['setup_minutes']['max'])}, "
            f"setup skipped {number(100 * impl['setup_skipped_share'] if impl['setup_skipped_share'] is not None else None)}%",
        ]
        for role, rows in group["commands"].items():
            lines += ["", f"top commands, {role}:"]
            lines += [f"  {row['minutes']:>8.1f} min {row['calls']:>5}x  {row['command']}" for row in rows]
        lines.append("")
    return "\n".join(lines)


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def read_transcript(path: Path) -> list[dict]:
    entries = []
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)):
                    entries.append(entry)
    except OSError:
        pass
    return entries


def load_runs(root: Path, task_id: str, transcripts: bool = True) -> list[dict]:
    runs = []
    base = root / "artifacts" / task_id / "browser"
    if not base.is_dir():
        return runs
    for folder in base.iterdir():
        execution = read_json(folder / "execution.json")
        if not folder.is_dir() or not isinstance(execution, dict):
            continue
        # The factory page skips transcripts: they are large and only the command tables need them.
        entries = read_transcript(folder / "transcript.jsonl") if transcripts else []
        runs.append(
            {
                "generation": folder.name,
                "role": execution.get("role"),
                "profile": execution.get("profile"),
                "model": execution.get("model"),
                "usage": read_json(folder / "usage.json"),
                "entries": entries,
                **run_profile(entries),
                # Without the transcript the run record says when it started and whether it resumed.
                **(
                    {}
                    if transcripts
                    else {
                        "start": execution.get("started_at"),
                        "resumed": bool(execution.get("resumed")),
                        "agent": execution.get("agent"),
                    }
                ),
            }
        )
    return runs


def load_tasks(
    last: int, since: float | None, project_id: str | None = None, transcripts: bool = True
) -> list[dict]:
    from sqlalchemy import select

    from darkfactory.db import ROOT, Event, Session, Task

    with Session() as db:
        query = select(Task).where(Task.system_check.is_(False))
        if project_id is not None:
            query = query.where(Task.project_id == project_id)
        if since is not None:
            query = query.where(Task.created >= since)
        tasks = db.scalars(query.order_by(Task.created.desc()).limit(last)).all()
        grouped = {}
        for task_id, at, kind, message in db.execute(
            select(Event.task_id, Event.at, Event.kind, Event.message)
            .where(Event.task_id.in_([task.id for task in tasks]))
            .order_by(Event.at)
        ):
            grouped.setdefault(task_id, []).append((at, kind, message))
        result = []
        for task in tasks:
            events = grouped.get(task.id, [])
            result.append(
                {
                    "id": task.id,
                    "state": task.state,
                    "corrections": task.corrections,
                    "events": [tuple(row) for row in events],
                    "runs": load_runs(ROOT, task.id, transcripts),
                }
            )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--last", type=int, default=100, help="last N non-system tasks by creation time")
    parser.add_argument("--since", help="only tasks created at or after this ISO time")
    parser.add_argument("--json", action="store_true", help="print JSON instead of tables")
    args = parser.parse_args(argv)
    since = datetime.fromisoformat(args.since).timestamp() if args.since else None
    data = report(load_tasks(args.last, since))
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
