"""Lightweight Meridian task dispatcher and status helpers.

The Meridian task queue is file-based. The queue directories under ``tasks/``
are the source of truth for work ownership, while
``$HERMES_HOME/meridian/workflow_state.json`` stores orchestration metadata
such as the last transition timestamp and dispatch bookkeeping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from utils import atomic_json_write


QUEUE_NAMES = ("backlog", "ready", "in_progress", "review", "done", "debt")
DISPATCHABLE_PERSONAS = frozenset({"philip", "fatih", "matthew"})
STATE_PATH = get_hermes_home() / "meridian" / "workflow_state.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def _task_dirs(workspace: Path) -> dict[str, Path]:
    tasks_root = workspace / "tasks"
    return {name: tasks_root / name for name in QUEUE_NAMES}


def _list_task_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )


def _extract_field(text: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix):
            value = stripped[len(prefix):].strip().strip("'\"")
            return value or None
    return None


@dataclass(frozen=True)
class TaskRef:
    queue: str
    path: Path
    task_id: str
    filename: str
    branch: str | None = None


def _read_task_ref(queue: str, path: Path) -> TaskRef:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    task_id = _extract_field(text, "id") or path.stem
    branch = _extract_field(text, "pr_branch") or _extract_field(text, "branch")
    return TaskRef(
        queue=queue,
        path=path,
        task_id=task_id,
        filename=path.name,
        branch=branch,
    )


def _queue_map(workspace: Path) -> dict[str, list[TaskRef]]:
    queues: dict[str, list[TaskRef]] = {}
    for name, directory in _task_dirs(workspace).items():
        queues[name] = [_read_task_ref(name, path) for path in _list_task_files(directory)]
    return queues


def _pick_first(tasks: list[TaskRef]) -> TaskRef | None:
    return tasks[0] if tasks else None


def _pick_matching(tasks: list[TaskRef], task_id: str | None) -> TaskRef | None:
    if not task_id:
        return None
    for task in tasks:
        if task.task_id == task_id or task.filename == task_id:
            return task
    return None


def _queue_counts(queues: dict[str, list[TaskRef]]) -> dict[str, int]:
    return {name: len(queues[name]) for name in QUEUE_NAMES}


def _normalize_waiting_human(state: dict[str, Any]) -> bool:
    return bool(state.get("waiting_human")) or state.get("waiting_on") == "human_confirmation"


def _review_loop_task_id(queues: dict[str, list[TaskRef]], state: dict[str, Any]) -> str | None:
    review_task = _pick_first(queues["review"])
    if review_task:
        return review_task.task_id

    prior = state.get("review_loop_task_id")
    if _pick_matching(queues["in_progress"], prior):
        return prior
    if _pick_matching(queues["ready"], prior):
        return prior
    return None


def collect_meridian_snapshot(workspace: str | Path | None = None, *, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect a Meridian workflow snapshot from the task queues."""
    workspace_path = Path(workspace or ".").resolve()
    state = dict(state or _read_json(STATE_PATH))
    queues = _queue_map(workspace_path)
    review_loop_task_id = _review_loop_task_id(queues, state)
    waiting_human = _normalize_waiting_human(state)

    active_persona = "idle"
    workflow_state = "idle"
    waiting_on = "none"
    active_task: TaskRef | None = None

    if waiting_human:
        active_persona = "idle"
        workflow_state = "waiting_human"
        waiting_on = "human_confirmation"
        active_task = (
            _pick_matching(queues["review"], review_loop_task_id)
            or _pick_matching(queues["in_progress"], review_loop_task_id)
            or _pick_matching(queues["ready"], review_loop_task_id)
        )
    elif queues["review"]:
        active_persona = "matthew"
        workflow_state = "review"
        waiting_on = "matthew"
        active_task = _pick_first(queues["review"])
        review_loop_task_id = active_task.task_id if active_task else review_loop_task_id
    elif queues["in_progress"]:
        active_persona = "fatih"
        workflow_state = "in_progress"
        waiting_on = "fatih"
        active_task = _pick_matching(queues["in_progress"], review_loop_task_id) or _pick_first(queues["in_progress"])
    elif queues["ready"]:
        active_persona = "fatih"
        workflow_state = "ready"
        waiting_on = "fatih"
        active_task = _pick_matching(queues["ready"], review_loop_task_id) or _pick_first(queues["ready"])
    elif queues["backlog"]:
        active_persona = "philip"
        workflow_state = "backlog"
        waiting_on = "philip"
        active_task = _pick_first(queues["backlog"])

    return {
        "workspace": str(workspace_path),
        "tasks_root": str((workspace_path / "tasks").resolve()),
        "active_persona": active_persona,
        "active_task_id": active_task.task_id if active_task else None,
        "active_task_filename": active_task.filename if active_task else None,
        "workflow_state": workflow_state,
        "waiting_on": waiting_on,
        "queue_counts": _queue_counts(queues),
        "waiting_human": waiting_human,
        "review_loop_task_id": review_loop_task_id,
        "current_branch": active_task.branch if active_task else None,
    }


def persist_meridian_snapshot(snapshot: dict[str, Any], *, previous_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist orchestration metadata while preserving dispatch bookkeeping."""
    previous_state = dict(previous_state or _read_json(STATE_PATH))
    transition_keys = (
        "active_persona",
        "active_task_id",
        "active_task_filename",
        "workflow_state",
        "waiting_on",
        "waiting_human",
        "review_loop_task_id",
        "current_branch",
    )
    changed = any(previous_state.get(key) != snapshot.get(key) for key in transition_keys)
    last_transition_time = previous_state.get("last_transition_time") or _utcnow_iso()
    if changed:
        last_transition_time = _utcnow_iso()

    merged = {
        **previous_state,
        **snapshot,
        "last_transition_time": last_transition_time,
    }
    atomic_json_write(STATE_PATH, merged)
    return merged


def _dispatch_target_changed(state: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    return any(
        state.get(key) != snapshot.get(key)
        for key in ("active_persona", "active_task_id", "workflow_state", "waiting_on")
    )


def dispatch_meridian(workspace: str | Path | None = None) -> dict[str, Any]:
    """Run one manual dispatch pass and update orchestration metadata."""
    previous_state = _read_json(STATE_PATH)
    snapshot = collect_meridian_snapshot(workspace, state=previous_state)
    merged = persist_meridian_snapshot(snapshot, previous_state=previous_state)

    should_dispatch = (
        snapshot["active_persona"] in DISPATCHABLE_PERSONAS
        and snapshot["waiting_on"] in DISPATCHABLE_PERSONAS
        and not snapshot["waiting_human"]
        and (
            previous_state.get("last_dispatched_persona") != snapshot["active_persona"]
            or previous_state.get("last_dispatched_task_id") != snapshot["active_task_id"]
            or previous_state.get("last_dispatched_transition_time") != merged["last_transition_time"]
            or _dispatch_target_changed(previous_state, snapshot)
        )
    )

    if should_dispatch:
        merged.update(
            {
                "last_dispatched_persona": snapshot["active_persona"],
                "last_dispatched_task_id": snapshot["active_task_id"],
                "last_dispatched_transition_time": merged["last_transition_time"],
                "last_dispatch_at": _utcnow_iso(),
            }
        )
        atomic_json_write(STATE_PATH, merged)

    merged["should_dispatch"] = should_dispatch
    return merged


def _print_status(snapshot: dict[str, Any]) -> None:
    queue_counts = snapshot["queue_counts"]
    print("Meridian status")
    print(f"  Active persona: {snapshot['active_persona']}")
    if snapshot.get("active_task_id") or snapshot.get("active_task_filename"):
        print(
            "  Active task:    "
            f"{snapshot.get('active_task_id') or '-'}"
            f" / {snapshot.get('active_task_filename') or '-'}"
        )
    else:
        print("  Active task:    -")
    print(f"  Workflow state: {snapshot['workflow_state']}")
    print(f"  Waiting on:     {snapshot['waiting_on']}")
    print(
        "  Queue counts:   "
        f"backlog={queue_counts['backlog']} "
        f"ready={queue_counts['ready']} "
        f"in_progress={queue_counts['in_progress']} "
        f"review={queue_counts['review']} "
        f"done={queue_counts['done']} "
        f"debt={queue_counts['debt']}"
    )
    print(f"  Last transition:{' ' if snapshot.get('last_transition_time') else ''}{snapshot.get('last_transition_time', '-')}")
    if snapshot.get("current_branch"):
        print(f"  Current branch: {snapshot['current_branch']}")


def meridian_command(args) -> int:
    """Entry point for ``hermes meridian`` commands."""
    workspace = getattr(args, "workspace", None) or "."
    subcommand = getattr(args, "meridian_command", None) or "status"

    if subcommand == "dispatch":
        snapshot = dispatch_meridian(workspace)
        _print_status(snapshot)
        if snapshot["should_dispatch"]:
            print(
                f"\nDispatch suggestion: wake {snapshot['active_persona']} "
                f"for {snapshot.get('active_task_id') or snapshot.get('active_task_filename') or 'the next task'}."
            )
        elif snapshot["workflow_state"] == "waiting_human":
            print("\nDispatch suggestion: hold. Waiting for human confirmation.")
        else:
            print("\nDispatch suggestion: no new wake-up needed.")
        return 0

    snapshot = persist_meridian_snapshot(
        collect_meridian_snapshot(workspace),
    )
    _print_status(snapshot)
    return 0
