"""Lightweight Meridian task dispatcher and status helpers.

The Meridian task queue is file-based. The queue directories under ``tasks/``
are the source of truth for work ownership, while
``$HERMES_HOME/meridian/workflow_state.json`` stores orchestration metadata
such as the last transition timestamp and dispatch bookkeeping.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.meridian_runtime import (
    EVENT_LOG_PATH,
    acquire_orchestrator_lease,
    act_on_planned_actions,
    build_snapshot_version,
    emit_meridian_event,
    finalize_orchestrator_lease,
    isoformat as runtime_isoformat,
    parse_iso_datetime,
    prune_expired_worker_leases,
    utcnow as runtime_utcnow,
)
from hermes_cli.meridian_workflow import (
    DISPATCH_VISIBLE_QUEUES,
    TaskRef,
    list_task_refs,
    locate_task,
)
from hermes_cli.config import load_config
from hermes_utils import atomic_json_write


DISPATCHABLE_PERSONAS = frozenset({"philip", "fatih", "matthew"})
READY_TARGET_DEFAULT = 2
STALE_TIMEOUTS = {
    "in_progress": timedelta(hours=48),
    "review": timedelta(hours=24),
    "waiting_human": timedelta(hours=72),
}
PRIORITY_RANK = {
    "critical": 0,
    "p0": 0,
    "high": 1,
    "p1": 1,
    "medium": 2,
    "normal": 2,
    "p2": 2,
    "low": 3,
    "p3": 3,
    "debt": 4,
}
STATE_PATH = get_hermes_home() / "meridian" / "workflow_state.json"
AUTO_DISCOVERY_CANDIDATES = (
    "meridian",
    "Meridian",
    "workspace/meridian",
    "Projects/meridian",
    "code/meridian",
)


def _terminal_remote_hint() -> tuple[str | None, str | None]:
    try:
        config = load_config()
    except Exception:
        return None, None
    terminal = config.get("terminal") or {}
    backend = str(terminal.get("backend") or "").strip().lower()
    if backend != "ssh":
        return None, None
    return (
        str(terminal.get("ssh_host") or "").strip() or None,
        str(terminal.get("cwd") or "").strip() or None,
    )


def _ensure_local_workspace_exists(workspace_path: Path) -> None:
    if workspace_path.exists():
        return
    ssh_host, remote_cwd = _terminal_remote_hint()
    if ssh_host:
        remote_note = f" Remote terminal backend points to {ssh_host}:{remote_cwd or '~'}."
    else:
        remote_note = ""
    raise FileNotFoundError(
        f"Meridian workspace does not exist on this machine: {workspace_path}.{remote_note} "
        "Run this command on the machine that has the Meridian checkout, or provide a local workspace path."
    )


def _resolve_workspace_path(workspace: str | Path | None = None) -> Path:
    explicit = str(workspace).strip() if workspace is not None else ""
    if explicit and explicit != ".":
        candidate = Path(explicit).expanduser().resolve()
        _ensure_local_workspace_exists(candidate)
        return candidate

    env_workspace = (os.getenv("HERMES_MERIDIAN_WORKSPACE") or "").strip()
    if env_workspace:
        candidate = Path(env_workspace).expanduser().resolve()
        if (candidate / "tasks").is_dir():
            return candidate

    state = _read_json(STATE_PATH)
    state_workspace = str(state.get("workspace") or "").strip()
    if state_workspace:
        candidate = Path(state_workspace).expanduser().resolve()
        if (candidate / "tasks").is_dir():
            return candidate

    cwd = Path.cwd().resolve()
    if (cwd / "tasks").is_dir():
        return cwd

    home = Path.home()
    for rel in AUTO_DISCOVERY_CANDIDATES:
        candidate = (home / rel).resolve()
        if (candidate / "tasks").is_dir():
            return candidate

    task_roots: list[Path] = []
    try:
        for child in home.iterdir():
            if not child.is_dir():
                continue
            if (child / "tasks").is_dir():
                task_roots.append(child.resolve())
    except OSError:
        pass

    def _candidate_rank(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        exact_meridian = 0 if name == "meridian" else 1
        contains_meridian = 0 if "meridian" in str(path).lower() else 1
        return (exact_meridian, contains_meridian, str(path))

    if task_roots:
        return sorted(task_roots, key=_candidate_rank)[0]

    if explicit:
        _ensure_local_workspace_exists(Path(explicit).expanduser().resolve())
    return cwd


def _local_policy_window(now: datetime) -> str:
    local_now = now.astimezone()
    hour = local_now.hour
    if 3 <= hour < 6:
        return "night_patrol"
    if 6 <= hour < 8:
        return "philip_morning"
    return "normal"


def _utcnow_iso() -> str:
    return runtime_isoformat(runtime_utcnow())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def _queue_map(workspace: Path) -> dict[str, list[TaskRef]]:
    return list_task_refs(workspace)


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
    return {name: len(queues.get(name, [])) for name in DISPATCH_VISIBLE_QUEUES}


def _task_metadata(task: TaskRef) -> dict[str, Any]:
    return dict(task.metadata or {})


def _task_updated_at(task: TaskRef) -> datetime:
    metadata = _task_metadata(task)
    for key in ("updated_at", "last_transition_at", "claimed_at", "created_at"):
        parsed = parse_iso_datetime(metadata.get(key))
        if parsed:
            return parsed
    return datetime.fromtimestamp(task.path.stat().st_mtime, tz=timezone.utc)


def _task_created_at(task: TaskRef) -> datetime:
    metadata = _task_metadata(task)
    for key in ("created_at", "updated_at", "last_transition_at"):
        parsed = parse_iso_datetime(metadata.get(key))
        if parsed:
            return parsed
    return datetime.fromtimestamp(task.path.stat().st_mtime, tz=timezone.utc)


def _priority_rank(task: TaskRef) -> tuple[int, str]:
    raw = _task_metadata(task).get("priority", "")
    if isinstance(raw, (int, float)):
        return int(raw), str(raw)
    normalized = str(raw).strip().lower()
    return PRIORITY_RANK.get(normalized, 9), normalized


def _depends_on(task: TaskRef) -> list[str]:
    raw = _task_metadata(task).get("depends_on")
    if raw in (None, "", []):
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [str(raw).strip()]


def _dependencies_ready(task: TaskRef, done_ids: set[str]) -> bool:
    deps = _depends_on(task)
    return all(dep in done_ids for dep in deps)


def _is_promotable_backlog_task(task: TaskRef, done_ids: set[str]) -> bool:
    metadata = _task_metadata(task)
    acceptance = metadata.get("acceptance_criteria")
    if acceptance in (None, "", []):
        return False
    if metadata.get("blocked_reason") or metadata.get("waiting_on"):
        return False
    return _dependencies_ready(task, done_ids)


def _parse_duration(value: Any) -> timedelta | None:
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    unit_map = {
        "h": 3600,
        "hr": 3600,
        "hrs": 3600,
        "hour": 3600,
        "hours": 3600,
        "d": 86400,
        "day": 86400,
        "days": 86400,
        "m": 60,
        "min": 60,
        "mins": 60,
        "minute": 60,
        "minutes": 60,
    }
    for suffix, seconds in unit_map.items():
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            try:
                return timedelta(seconds=float(number) * seconds)
            except ValueError:
                return None
    return None


def _stale_deadline(task: TaskRef) -> datetime | None:
    metadata = _task_metadata(task)
    raw = metadata.get("stale_after")
    parsed_time = parse_iso_datetime(raw)
    if parsed_time:
        return parsed_time
    parsed_duration = _parse_duration(raw)
    if parsed_duration:
        return _task_updated_at(task) + parsed_duration
    timeout = STALE_TIMEOUTS.get(task.queue)
    if timeout is None:
        return None
    return _task_updated_at(task) + timeout


def _stale_reason(task: TaskRef) -> str:
    if task.queue == "review":
        return "review_sla_exceeded"
    if task.queue == "waiting_human":
        return "human_confirmation_overdue"
    return "implementation_stale"


def _detect_stale_tasks(queues: dict[str, list[TaskRef]], *, now: datetime) -> list[dict[str, Any]]:
    stale_entries: list[dict[str, Any]] = []
    for queue in ("in_progress", "review", "waiting_human"):
        for task in queues.get(queue, []):
            deadline = _stale_deadline(task)
            if deadline and deadline <= now:
                stale_entries.append(
                    {
                        "task_id": task.task_id,
                        "queue": queue,
                        "reason": _stale_reason(task),
                        "updated_at": _task_updated_at(task).isoformat(),
                        "stale_since": deadline.isoformat(),
                        "actor": "matthew" if queue != "waiting_human" else "human",
                    }
                )
    stale_entries.sort(key=lambda entry: (entry["stale_since"], entry["task_id"]))
    return stale_entries


def _select_task(tasks: list[TaskRef], *, preferred_task_id: str | None = None) -> TaskRef | None:
    if not tasks:
        return None

    def sort_key(task: TaskRef) -> tuple[int, int, datetime, str]:
        preferred_rank = 0 if preferred_task_id and task.task_id == preferred_task_id else 1
        priority_rank, _ = _priority_rank(task)
        return (preferred_rank, priority_rank, _task_created_at(task), task.task_id)

    return sorted(tasks, key=sort_key)[0]


def _build_planned_actions(
    queues: dict[str, list[TaskRef]],
    *,
    state: dict[str, Any],
    now: datetime,
    ready_target: int,
    review_loop_task_id: str | None,
    waiting_human: bool,
    policy_window: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    stale_entries = _detect_stale_tasks(queues, now=now)
    stale_by_task = {entry["task_id"]: entry for entry in stale_entries}
    done_ids = {task.task_id for task in queues.get("done", [])}

    if waiting_human:
        waiting_task = _pick_first(queues.get("waiting_human", []))
        actions.append(
            {
                "kind": "hold_waiting_human",
                "actor": "human",
                "task_id": waiting_task.task_id if waiting_task else review_loop_task_id,
                "queue": "waiting_human",
                "reason": "human_confirmation_required",
                "dispatchable": False,
            }
        )

    review_task = _select_task(
        queues.get("review", []),
        preferred_task_id=review_loop_task_id,
    )
    if review_task:
        stale = stale_by_task.get(review_task.task_id)
        actions.append(
            {
                "kind": "review_task" if not stale else "triage_stale_review",
                "actor": "matthew",
                "task_id": review_task.task_id,
                "queue": "review",
                "reason": stale["reason"] if stale else "review_queue_non_empty",
                "dispatchable": True,
                "stale": bool(stale),
            }
        )

    in_progress_task = _select_task(
        queues.get("in_progress", []),
        preferred_task_id=review_loop_task_id,
    )
    fatih_can_start_new_work = policy_window not in {"night_patrol", "philip_morning"}
    if in_progress_task:
        stale = stale_by_task.get(in_progress_task.task_id)
        actions.append(
            {
                "kind": "continue_review_loop" if review_loop_task_id == in_progress_task.task_id else "continue_in_progress",
                "actor": "fatih" if not stale else "matthew",
                "task_id": in_progress_task.task_id,
                "queue": "in_progress",
                "reason": stale["reason"] if stale else "active_delivery_work",
                "dispatchable": not waiting_human if not stale else True,
                "stale": bool(stale),
            }
        )
    elif queues.get("ready"):
        ready_task = _select_task(
            queues["ready"],
            preferred_task_id=review_loop_task_id,
        )
        if ready_task:
            actions.append(
                {
                    "kind": "start_ready_task",
                    "actor": "fatih",
                    "task_id": ready_task.task_id,
                    "queue": "ready",
                    "reason": "ready_queue_non_empty",
                    "dispatchable": not waiting_human and fatih_can_start_new_work,
                }
            )

    ready_count = len(queues.get("ready", []))
    promotable = [
        task
        for task in queues.get("backlog", [])
        if _is_promotable_backlog_task(task, done_ids)
    ]
    promotable = sorted(
        promotable,
        key=lambda task: (_priority_rank(task)[0], _task_created_at(task), task.task_id),
    )
    if ready_count < ready_target and promotable:
        deficit = min(ready_target - ready_count, len(promotable))
        actions.append(
            {
                "kind": "replenish_ready",
                "actor": "philip",
                "queue": "backlog",
                "task_id": promotable[0].task_id,
                "task_ids": [task.task_id for task in promotable[:deficit]],
                "reason": "ready_below_target",
                "dispatchable": True,
                "deficit": deficit,
            }
        )
    elif not queues.get("review") and not queues.get("in_progress") and not queues.get("ready"):
        backlog_task = _select_task(queues.get("backlog", []))
        if backlog_task:
            actions.append(
                {
                    "kind": "groom_backlog",
                    "actor": "philip",
                    "task_id": backlog_task.task_id,
                    "queue": "backlog",
                    "reason": "delivery_idle_backlog_available",
                    "dispatchable": True,
                }
            )
        elif queues.get("debt"):
            debt_task = _select_task(queues["debt"])
            actions.append(
                {
                    "kind": "triage_debt",
                    "actor": "philip",
                    "task_id": debt_task.task_id if debt_task else None,
                    "queue": "debt",
                    "reason": "delivery_idle_debt_available",
                    "dispatchable": True,
                }
            )

    waiting_human_stale = [
        entry for entry in stale_entries if entry["queue"] == "waiting_human"
    ]
    if waiting_human_stale:
        actions.append(
            {
                "kind": "remind_waiting_human",
                "actor": "human",
                "task_id": waiting_human_stale[0]["task_id"],
                "queue": "waiting_human",
                "reason": waiting_human_stale[0]["reason"],
                "dispatchable": False,
                "stale": True,
            }
        )

    delivery_active = bool(
        queues.get("review") or queues.get("in_progress") or queues.get("ready")
    )
    philip_already_planned = any(action.get("actor") == "philip" for action in actions)
    if delivery_active and not philip_already_planned:
        backlog_task = _select_task(queues.get("backlog", []))
        if backlog_task:
            actions.append(
                {
                    "kind": "background_backlog_scan",
                    "actor": "philip",
                    "task_id": backlog_task.task_id,
                    "queue": "backlog",
                    "reason": "delivery_active_keep_backlog_warm",
                    "dispatchable": True,
                }
            )
        elif queues.get("debt"):
            debt_task = _select_task(queues["debt"])
            actions.append(
                {
                    "kind": "background_debt_triage",
                    "actor": "philip",
                    "task_id": debt_task.task_id if debt_task else None,
                    "queue": "debt",
                    "reason": "delivery_active_keep_debt_visible",
                    "dispatchable": True,
                }
            )

    matthew_already_planned = any(action.get("actor") == "matthew" for action in actions)
    if policy_window == "night_patrol" and not matthew_already_planned and not queues.get("in_progress"):
        patrol_target = _select_task(queues.get("backlog", [])) or _select_task(queues.get("debt", []))
        actions.append(
            {
                "kind": "night_architecture_patrol",
                "actor": "matthew",
                "task_id": patrol_target.task_id if patrol_target else None,
                "queue": patrol_target.queue if patrol_target else "workspace",
                "reason": "night_patrol_architecture_and_security_review",
                "dispatchable": True,
            }
        )

    philip_already_planned = any(action.get("actor") == "philip" for action in actions)
    if policy_window in {"night_patrol", "philip_morning"} and not philip_already_planned:
        planning_target = _select_task(queues.get("backlog", [])) or _select_task(queues.get("debt", []))
        actions.append(
            {
                "kind": "night_backlog_planning" if policy_window == "night_patrol" else "morning_backlog_planning",
                "actor": "philip",
                "task_id": planning_target.task_id if planning_target else None,
                "queue": planning_target.queue if planning_target else "workspace",
                "reason": (
                    "night_patrol_backlog_and_feature_shaping"
                    if policy_window == "night_patrol"
                    else "morning_backlog_planning_window"
                ),
                "dispatchable": True,
            }
        )

    return actions


def _normalize_waiting_human(state: dict[str, Any]) -> bool:
    return bool(state.get("waiting_human")) or state.get("waiting_on") == "human_confirmation"


def _review_loop_task_id(queues: dict[str, list[TaskRef]], state: dict[str, Any]) -> str | None:
    review_task = _pick_first(queues["review"])
    if review_task:
        return review_task.task_id

    waiting_human_task = _pick_first(queues["waiting_human"])
    if waiting_human_task:
        return waiting_human_task.task_id

    prior = state.get("review_loop_task_id")
    if _pick_matching(queues["in_progress"], prior):
        return prior
    if _pick_matching(queues["ready"], prior):
        return prior
    return None


def collect_meridian_snapshot(
    workspace: str | Path | None = None,
    *,
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect a Meridian workflow snapshot from the task queues."""
    workspace_path = Path(workspace or ".").resolve()
    workspace_path = _resolve_workspace_path(workspace)
    state = dict(state or _read_json(STATE_PATH))
    queues = _queue_map(workspace_path)
    current_time = now or runtime_utcnow()
    policy_window = _local_policy_window(current_time)
    ready_target = int(state.get("ready_target") or READY_TARGET_DEFAULT)
    review_loop_task_id = _review_loop_task_id(queues, state)
    waiting_human_task = _pick_first(queues["waiting_human"])
    waiting_human = bool(waiting_human_task)
    stale_tasks = _detect_stale_tasks(queues, now=current_time)
    planned_actions = _build_planned_actions(
        queues,
        state=state,
        now=current_time,
        ready_target=ready_target,
        review_loop_task_id=review_loop_task_id,
        waiting_human=waiting_human,
        policy_window=policy_window,
    )

    active_persona = "idle"
    workflow_state = "idle"
    waiting_on = "none"
    active_task: TaskRef | None = None

    if waiting_human:
        active_persona = "idle"
        workflow_state = "waiting_human"
        waiting_on = "human_confirmation"
        active_task = (
            waiting_human_task
            or _pick_matching(queues["review"], review_loop_task_id)
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
        "ready_target": ready_target,
        "policy_window": policy_window,
        "planned_actions": planned_actions,
        "stale_tasks": stale_tasks,
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
        "planned_actions",
        "stale_tasks",
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


def dispatch_meridian(
    workspace: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one manual dispatch pass and update orchestration metadata."""
    previous_state = _read_json(STATE_PATH)
    current_time = now or runtime_utcnow()
    snapshot = collect_meridian_snapshot(workspace, state=previous_state, now=current_time)
    merged = persist_meridian_snapshot(snapshot, previous_state=previous_state)

    acquired, orchestrator_lease, lease_reason = acquire_orchestrator_lease(
        merged,
        workspace=snapshot["workspace"],
        now=current_time,
    )
    merged["snapshot_version"] = build_snapshot_version(snapshot)

    if not acquired:
        merged["should_dispatch"] = False
        merged["dispatch_blocked_reason"] = lease_reason
        merged["orchestrator_lease"] = orchestrator_lease
        atomic_json_write(STATE_PATH, merged)
        return merged

    dispatched_actions, suppressed_actions, worker_leases = act_on_planned_actions(
        snapshot,
        state=merged,
        run_id=orchestrator_lease["run_id"],
        now=current_time,
    )

    merged["orchestrator_lease"] = finalize_orchestrator_lease(
        orchestrator_lease,
        now=current_time,
    )
    merged["worker_leases"] = worker_leases
    merged["last_dispatch_results"] = {
        "run_id": orchestrator_lease["run_id"],
        "snapshot_version": merged["snapshot_version"],
        "dispatched_actions": dispatched_actions,
        "suppressed_actions": suppressed_actions,
        "completed_at": runtime_isoformat(current_time),
    }

    should_dispatch = bool(dispatched_actions)
    if dispatched_actions:
        primary = dispatched_actions[0]
        merged.update(
            {
                "last_dispatched_persona": primary["actor"],
                "last_dispatched_task_id": primary.get("task_id"),
                "last_dispatched_transition_time": merged["last_transition_time"],
                "last_dispatch_at": runtime_isoformat(current_time),
            }
        )

    merged["should_dispatch"] = should_dispatch
    atomic_json_write(STATE_PATH, merged)
    emit_meridian_event(
        "meridian_dispatch_completed",
        {
            "workspace": snapshot["workspace"],
            "run_id": orchestrator_lease["run_id"],
            "snapshot_version": merged["snapshot_version"],
            "dispatched_count": len(dispatched_actions),
            "suppressed_count": len(suppressed_actions),
        },
        now=current_time,
    )
    return merged


def reconcile_meridian(
    workspace: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rebuild derived Meridian state and emit reconcile/drift events."""
    previous_state = _read_json(STATE_PATH)
    current_time = now or runtime_utcnow()
    snapshot = collect_meridian_snapshot(workspace, state=previous_state, now=current_time)
    merged = persist_meridian_snapshot(snapshot, previous_state=previous_state)
    previous_version = previous_state.get("snapshot_version")
    merged["snapshot_version"] = build_snapshot_version(snapshot)
    merged["worker_leases"] = prune_expired_worker_leases(merged, now=current_time)

    drift_detected = bool(previous_version and previous_version != merged["snapshot_version"])
    reconcile_event = emit_meridian_event(
        "meridian_reconciled",
        {
            "workspace": snapshot["workspace"],
            "snapshot_version": merged["snapshot_version"],
            "drift_detected": drift_detected,
            "queue_counts": merged.get("queue_counts"),
        },
        now=current_time,
    )
    merged["last_reconcile_at"] = runtime_isoformat(current_time)
    merged["last_reconcile_event_id"] = reconcile_event["id"]
    merged["drift_detected"] = drift_detected
    if drift_detected:
        drift_event = emit_meridian_event(
            "meridian_drift_detected",
            {
                "workspace": snapshot["workspace"],
                "previous_snapshot_version": previous_version,
                "snapshot_version": merged["snapshot_version"],
            },
            now=current_time,
        )
        merged["last_drift_event_id"] = drift_event["id"]
    stale_signatures: dict[str, str] = {}
    previous_stale_signatures = previous_state.get("stale_event_signatures") or {}
    if not isinstance(previous_stale_signatures, dict):
        previous_stale_signatures = {}
    for stale_entry in merged.get("stale_tasks", []):
        signature = json.dumps(
            {
                "queue": stale_entry.get("queue"),
                "reason": stale_entry.get("reason"),
                "updated_at": stale_entry.get("updated_at"),
                "stale_since": stale_entry.get("stale_since"),
            },
            sort_keys=True,
        )
        task_id = stale_entry.get("task_id")
        if task_id:
            stale_signatures[task_id] = signature
            if previous_stale_signatures.get(task_id) == signature:
                continue
        emit_meridian_event(
            "stale_task_detected",
            {
                "workspace": snapshot["workspace"],
                **stale_entry,
            },
            now=current_time,
        )
    merged["stale_event_signatures"] = stale_signatures
    atomic_json_write(STATE_PATH, merged)
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
    if snapshot.get("stale_tasks"):
        print(f"  Stale tasks:    {len(snapshot['stale_tasks'])}")
    if snapshot.get("planned_actions"):
        first = snapshot["planned_actions"][0]
        print(
            "  Next action:    "
            f"{first.get('kind')} -> {first.get('actor')}"
            f" ({first.get('task_id') or first.get('queue') or '-'})"
        )
        if first.get("reason"):
            print(f"  Why now:        {first['reason']}")
    worker_leases = snapshot.get("worker_leases") or []
    if worker_leases:
        print(f"  Active leases:  {len(worker_leases)}")
    if snapshot.get("drift_detected"):
        print("  Drift detected: yes")
    recent_events = _recent_meridian_events(limit=5)
    if recent_events:
        print("  Recent events:")
        for event in recent_events:
            actor = event.get("actor") or event.get("waiting_on") or event.get("platform") or "-"
            task_id = event.get("task_id") or event.get("active_task_id") or "-"
            print(
                "    "
                f"{event.get('at') or '-'} "
                f"{event.get('type') or '-'} "
                f"actor={actor} "
                f"task={task_id}"
            )


def _recent_meridian_events(limit: int = 5) -> list[dict[str, Any]]:
    try:
        lines = EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for raw in reversed(lines):
        if len(events) >= limit:
            break
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _print_stale(snapshot: dict[str, Any]) -> None:
    print("Meridian stale tasks")
    stale_tasks = snapshot.get("stale_tasks") or []
    if not stale_tasks:
        print("  None")
        return
    for entry in stale_tasks:
        print(
            "  "
            f"{entry.get('task_id') or '-'} "
            f"[{entry.get('queue') or '-'}] "
            f"reason={entry.get('reason') or '-'} "
            f"stale_since={entry.get('stale_since') or '-'}"
        )


def _print_leases(snapshot: dict[str, Any]) -> None:
    print("Meridian leases")
    orchestrator = snapshot.get("orchestrator_lease")
    if orchestrator:
        print(
            "  Orchestrator:   "
            f"run_id={orchestrator.get('run_id') or '-'} "
            f"status={orchestrator.get('status') or '-'} "
            f"expires_at={orchestrator.get('expires_at') or '-'}"
        )
    else:
        print("  Orchestrator:   none")

    worker_leases = snapshot.get("worker_leases") or []
    if not worker_leases:
        print("  Worker leases:  none")
        return
    print(f"  Worker leases:  {len(worker_leases)}")
    for lease in worker_leases:
        print(
            "    "
            f"{lease.get('actor') or '-'} "
            f"task={lease.get('task_id') or '-'} "
            f"kind={lease.get('kind') or '-'} "
            f"expires_at={lease.get('expires_at') or '-'}"
        )


def _print_task_history(workspace: str | Path, task_id: str) -> None:
    print("Meridian task history")
    document = locate_task(workspace, task_id)
    print(f"  Task:           {document.task_id}")
    print(f"  Queue:          {document.queue}")
    print(f"  Status:         {document.metadata.get('status') or document.queue}")
    history = document.metadata.get("workflow_history") or []
    if not history:
        print("  History:        none")
        return
    print(f"  History:        {len(history)} event(s)")
    for entry in history:
        print(
            "    "
            f"{entry.get('at') or '-'} "
            f"{entry.get('event') or '-'} "
            f"actor={entry.get('actor') or '-'} "
            f"from={entry.get('from_queue') or entry.get('queue') or '-'} "
            f"to={entry.get('to_queue') or '-'} "
            f"reason={entry.get('reason') or '-'}"
        )


def _print_dispatch_summary(snapshot: dict[str, Any]) -> None:
    if snapshot["should_dispatch"]:
        actions = snapshot.get("last_dispatch_results", {}).get("dispatched_actions", [])
        first = actions[0] if actions else {}
        print(
            "  Dispatch:       "
            f"wake {first.get('actor') or snapshot['active_persona']} "
            f"for {first.get('task_id') or snapshot.get('active_task_id') or snapshot.get('active_task_filename') or 'the next task'}"
        )
        if len(actions) > 1:
            print(f"  More wakeups:    {len(actions) - 1}")
    elif snapshot["workflow_state"] == "waiting_human":
        print("  Dispatch:       hold (waiting for human confirmation)")
    elif snapshot.get("dispatch_blocked_reason") == "orchestrator_lease_active":
        print("  Dispatch:       hold (another orchestration pass owns the lease)")
    else:
        print("  Dispatch:       no new wake-up needed")


def run_meridian_go_loop(
    workspace: str | Path | None = None,
    *,
    sleep_seconds: float = 15.0,
    idle_sleep_seconds: float = 60.0,
    max_passes: int | None = None,
    once: bool = False,
) -> int:
    """Run a long-lived Meridian orchestration loop with gentle backoff."""
    workspace_path = _resolve_workspace_path(workspace)
    passes = 0
    last_snapshot_version: str | None = None
    last_dispatch_signature: tuple[Any, ...] | None = None

    print("Meridian go loop")
    print(f"  Workspace:      {workspace_path}")
    print(f"  Active sleep:   {sleep_seconds:.0f}s")
    print(f"  Idle sleep:     {idle_sleep_seconds:.0f}s")
    if once:
        print("  Mode:           once")
    elif max_passes is not None:
        print(f"  Max passes:     {max_passes}")
    else:
        print("  Mode:           continuous")

    try:
        while True:
            passes += 1
            reconcile_meridian(workspace_path)
            snapshot = dispatch_meridian(workspace_path)

            snapshot_version = build_snapshot_version(snapshot)
            dispatched_actions = snapshot.get("last_dispatch_results", {}).get("dispatched_actions", [])
            dispatch_signature = tuple(
                (item.get("actor"), item.get("task_id"), item.get("kind"), item.get("idempotency_key"))
                for item in dispatched_actions
            )
            should_report = (
                passes == 1
                or snapshot["should_dispatch"]
                or snapshot.get("dispatch_blocked_reason") == "orchestrator_lease_active"
                or snapshot_version != last_snapshot_version
                or dispatch_signature != last_dispatch_signature
            )

            if should_report:
                print(f"\nPass {passes}")
                _print_status(snapshot)
                _print_dispatch_summary(snapshot)

            if once:
                return 0
            if max_passes is not None and passes >= max_passes:
                print("\nMeridian go loop reached max passes and is stopping.")
                return 0

            delivery_active = snapshot["workflow_state"] in {"review", "in_progress", "ready", "backlog"}
            sleep_for = sleep_seconds if delivery_active else idle_sleep_seconds
            last_snapshot_version = snapshot_version
            last_dispatch_signature = dispatch_signature
            time.sleep(max(0.0, sleep_for))
    except KeyboardInterrupt:
        print("\nMeridian go loop stopped.")
        return 0


def meridian_command(args) -> int:
    """Entry point for ``hermes meridian`` commands."""
    workspace = getattr(args, "workspace", None)
    subcommand = getattr(args, "meridian_command", None) or "status"

    if subcommand == "go":
        return run_meridian_go_loop(
            workspace,
            sleep_seconds=float(getattr(args, "sleep", 15.0)),
            idle_sleep_seconds=float(getattr(args, "idle_sleep", 60.0)),
            max_passes=getattr(args, "max_passes", None),
            once=bool(getattr(args, "once", False)),
        )

    if subcommand == "dispatch":
        snapshot = dispatch_meridian(workspace)
        _print_status(snapshot)
        print()
        _print_dispatch_summary(snapshot)
        return 0

    if subcommand == "reconcile":
        snapshot = reconcile_meridian(workspace)
        _print_status(snapshot)
        if snapshot.get("drift_detected"):
            print("\nReconcile result: drift detected and derived state rebuilt from canonical task files.")
        else:
            print("\nReconcile result: derived state is aligned with canonical task files.")
        return 0

    if subcommand == "stale":
        snapshot = persist_meridian_snapshot(
            collect_meridian_snapshot(workspace),
        )
        _print_stale(snapshot)
        return 0

    if subcommand == "leases":
        snapshot = persist_meridian_snapshot(
            collect_meridian_snapshot(workspace),
        )
        _print_leases(snapshot)
        return 0

    if subcommand == "history":
        task_id = getattr(args, "task_id", None)
        if not task_id:
            print("Error: meridian history requires a task id.")
            return 1
        _print_task_history(workspace, task_id)
        return 0

    snapshot = persist_meridian_snapshot(
        collect_meridian_snapshot(workspace),
    )
    _print_status(snapshot)
    return 0
