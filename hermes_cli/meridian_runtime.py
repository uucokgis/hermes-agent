"""Derived Meridian runtime state: leases, idempotency, dispatch records, and events."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


ORCHESTRATOR_LEASE_TTL = timedelta(minutes=2)
WORKER_LEASE_TTL = timedelta(minutes=30)
EVENT_LOG_PATH = get_hermes_home() / "meridian" / "events.jsonl"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_snapshot_version(snapshot: dict[str, Any]) -> str:
    payload = {
        "workspace": snapshot.get("workspace"),
        "workflow_state": snapshot.get("workflow_state"),
        "active_task_id": snapshot.get("active_task_id"),
        "review_loop_task_id": snapshot.get("review_loop_task_id"),
        "queue_counts": snapshot.get("queue_counts"),
        "planned_actions": [
            {
                "kind": action.get("kind"),
                "actor": action.get("actor"),
                "task_id": action.get("task_id"),
                "task_ids": action.get("task_ids"),
                "queue": action.get("queue"),
                "reason": action.get("reason"),
            }
            for action in snapshot.get("planned_actions", [])
        ],
        "stale_tasks": [
            {
                "task_id": entry.get("task_id"),
                "queue": entry.get("queue"),
                "reason": entry.get("reason"),
            }
            for entry in snapshot.get("stale_tasks", [])
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def emit_meridian_event(
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    timestamp = now or utcnow()
    event = {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "at": isoformat(timestamp),
        **(payload or {}),
    }
    EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return event


def acquire_orchestrator_lease(
    state: dict[str, Any],
    *,
    workspace: str,
    now: datetime,
) -> tuple[bool, dict[str, Any], str | None]:
    existing = state.get("orchestrator_lease")
    if isinstance(existing, dict):
        expires_at = parse_iso_datetime(existing.get("expires_at"))
        if existing.get("status") == "active" and expires_at and expires_at > now:
            return False, existing, "orchestrator_lease_active"

    lease = {
        "run_id": uuid.uuid4().hex,
        "workspace": workspace,
        "acquired_at": isoformat(now),
        "expires_at": isoformat(now + ORCHESTRATOR_LEASE_TTL),
        "status": "active",
    }
    return True, lease, None


def finalize_orchestrator_lease(lease: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    return {
        **lease,
        "status": "completed",
        "completed_at": isoformat(now),
    }


def prune_expired_worker_leases(state: dict[str, Any], *, now: datetime) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for lease in state.get("worker_leases", []):
        if not isinstance(lease, dict):
            continue
        expires_at = parse_iso_datetime(lease.get("expires_at"))
        if expires_at and expires_at > now:
            active.append(lease)
    return active


def worker_action_identity(action: dict[str, Any]) -> str:
    actor = action.get("actor") or "unknown"
    task_or_queue = action.get("task_id") or action.get("queue") or "workspace"
    kind = action.get("kind") or "unknown"
    return f"{actor}:{task_or_queue}:{kind}"


def build_idempotency_key(
    *,
    workspace: str,
    action: dict[str, Any],
    snapshot_version: str,
) -> str:
    identity = worker_action_identity(action)
    encoded = f"{workspace}|{identity}|{snapshot_version}".encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def act_on_planned_actions(
    snapshot: dict[str, Any],
    *,
    state: dict[str, Any],
    run_id: str,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    workspace = str(Path(snapshot.get("workspace") or ".").resolve())
    snapshot_version = build_snapshot_version(snapshot)
    active_worker_leases = prune_expired_worker_leases(state, now=now)
    active_by_identity = {
        lease.get("action_identity"): lease
        for lease in active_worker_leases
        if isinstance(lease, dict) and lease.get("action_identity")
    }

    dispatched: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    next_worker_leases = list(active_worker_leases)
    seen_actors: set[str] = set()

    for action in snapshot.get("planned_actions", []):
        if not action.get("dispatchable"):
            continue
        actor = action.get("actor")
        if not actor or actor in seen_actors:
            continue
        seen_actors.add(actor)

        identity = worker_action_identity(action)
        idempotency_key = build_idempotency_key(
            workspace=workspace,
            action=action,
            snapshot_version=snapshot_version,
        )
        existing = active_by_identity.get(identity)
        if existing:
            suppressed.append(
                {
                    "actor": actor,
                    "task_id": action.get("task_id"),
                    "kind": action.get("kind"),
                    "reason": "worker_lease_active",
                    "idempotency_key": existing.get("idempotency_key"),
                    "lease_id": existing.get("lease_id"),
                }
            )
            continue

        lease = {
            "lease_id": uuid.uuid4().hex,
            "run_id": run_id,
            "actor": actor,
            "task_id": action.get("task_id"),
            "queue": action.get("queue"),
            "kind": action.get("kind"),
            "reason": action.get("reason"),
            "action_identity": identity,
            "snapshot_version": snapshot_version,
            "idempotency_key": idempotency_key,
            "acquired_at": isoformat(now),
            "expires_at": isoformat(now + WORKER_LEASE_TTL),
            "status": "active",
        }
        next_worker_leases.append(lease)
        dispatched.append(
            {
                "actor": actor,
                "task_id": action.get("task_id"),
                "queue": action.get("queue"),
                "kind": action.get("kind"),
                "reason": action.get("reason"),
                "idempotency_key": idempotency_key,
                "lease_id": lease["lease_id"],
                "run_id": run_id,
                "status": "dispatched",
            }
        )

    return dispatched, suppressed, next_worker_leases
