"""Deterministic Meridian task workflow primitives.

Task files under ``tasks/`` remain the canonical source of truth. This module
provides the official claim and transition APIs that synchronize queue
directories with task metadata and append structured workflow history.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_cli.meridian_runtime import emit_meridian_event


QUEUE_NAMES = (
    "backlog",
    "ready",
    "in_progress",
    "review",
    "waiting_human",
    "done",
    "debt",
)
LEGACY_QUEUE_ALIASES = {
    "in_progress": ("in-progress",),
}
QUEUE_SUBDIRECTORIES = {
    "review": ("review", "active"),
}
DISPATCH_VISIBLE_QUEUES = ("backlog", "ready", "in_progress", "review", "done", "debt")
PERSONA_QUEUES = {
    "philip": frozenset({"backlog", "ready", "debt"}),
    "fatih": frozenset({"ready", "in_progress", "review"}),
    "matthew": frozenset({"review", "waiting_human", "done", "in_progress"}),
    "human": frozenset({"waiting_human", "done", "in_progress"}),
}
TRANSITION_RULES = {
    "backlog": frozenset({"ready", "debt"}),
    "debt": frozenset({"backlog", "ready"}),
    "ready": frozenset({"in_progress", "backlog"}),
    "in_progress": frozenset({"review", "backlog"}),
    "review": frozenset({"done", "in_progress", "waiting_human"}),
    "waiting_human": frozenset({"in_progress", "done"}),
    "done": frozenset(),
}
EXCEPTIONAL_TRANSITIONS = {
    ("ready", "backlog"),
    ("in_progress", "backlog"),
}
TERMINAL_QUEUES = frozenset({"done"})
HISTORY_KEY = "workflow_history"


class MeridianWorkflowError(ValueError):
    """Raised when a Meridian task claim or transition is invalid."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _task_dirs(workspace: Path) -> dict[str, Path]:
    return {name: canonical_queue_dir(workspace, name) for name in QUEUE_NAMES}


def canonical_queue_dir(workspace: Path, queue: str) -> Path:
    tasks_root = workspace / "tasks"
    subpath = QUEUE_SUBDIRECTORIES.get(queue)
    if subpath:
        return tasks_root.joinpath(*subpath)
    return tasks_root / queue


def queue_dir_candidates(workspace: Path, queue: str) -> tuple[Path, ...]:
    candidates = [canonical_queue_dir(workspace, queue)]
    tasks_root = workspace / "tasks"
    if queue == "review":
        candidates.append(tasks_root / "review")
    names = LEGACY_QUEUE_ALIASES.get(queue, ())
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    for name in names:
        path = tasks_root / name
        if path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return tuple(ordered)


def workspace_root_from_task_path(path: Path) -> Path:
    for parent in path.parents:
        if parent.name == "tasks":
            return parent.parent
    raise MeridianWorkflowError(f"Unable to determine Meridian workspace root from task path: {path}")


def _coerce_metadata(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return dict(data)
    return {}


def _split_task_document(content: str) -> tuple[dict[str, Any], str]:
    if content.startswith("---\n"):
        closing = content.find("\n---\n", 4)
        if closing != -1:
            raw_frontmatter = content[4:closing]
            body = content[closing + 5 :]
            parsed = yaml.safe_load(raw_frontmatter) or {}
            return _coerce_metadata(parsed), body.lstrip("\n")

    lines = content.splitlines()
    metadata_lines: list[str] = []
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            body_start = index + 1
            break
        if ":" not in line:
            body_start = index
            break
        metadata_lines.append(line)
    else:
        body_start = len(lines)

    metadata: dict[str, Any] = {}
    if metadata_lines:
        parsed = yaml.safe_load("\n".join(metadata_lines)) or {}
        metadata = _coerce_metadata(parsed)

    body = "\n".join(lines[body_start:]).lstrip("\n")
    return metadata, body


def _render_task_document(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    parts = ["---", frontmatter, "---"]
    body = body.strip("\n")
    if body:
        parts.extend(["", body])
    return "\n".join(parts) + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _clear_empty_fields(metadata: dict[str, Any]) -> None:
    for key in (
        "blocked_reason",
        "waiting_on",
        "claim_expires_at",
    ):
        if metadata.get(key) in ("", None, []):
            metadata.pop(key, None)


def _append_history(metadata: dict[str, Any], entry: dict[str, Any]) -> None:
    history = metadata.get(HISTORY_KEY)
    if not isinstance(history, list):
        history = []
    history.append(entry)
    metadata[HISTORY_KEY] = history


def _normalize_actor(actor: str) -> str:
    normalized = (actor or "").strip().lower()
    if not normalized:
        raise MeridianWorkflowError("actor is required")
    return normalized


def _normalize_boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _verification_passed(metadata: dict[str, Any]) -> bool:
    status = str(metadata.get("verification_status") or "").strip().lower()
    if status in {"passed", "pass", "ok", "green"}:
        return True
    if _normalize_boolish(metadata.get("verification_passed")) is True:
        return True
    return False


def _require_review_handoff_metadata(document: "TaskDocument") -> None:
    metadata = document.metadata
    branch = str(metadata.get("pr_branch") or metadata.get("branch") or "").strip()
    commit_sha = str(metadata.get("commit_sha") or metadata.get("commit") or "").strip()
    verification_summary = str(metadata.get("verification_summary") or metadata.get("verification") or "").strip()

    missing: list[str] = []
    if not branch:
        missing.append("branch/pr_branch")
    if not commit_sha:
        missing.append("commit_sha")
    if not _verification_passed(metadata):
        missing.append("verification_status=passed")
    if not verification_summary:
        missing.append("verification_summary")

    if missing:
        raise MeridianWorkflowError(
            "in_progress -> review requires review handoff metadata: "
            + ", ".join(missing)
        )


def _require_review_completion_metadata(document: "TaskDocument") -> None:
    pushed = _normalize_boolish(document.metadata.get("pushed"))
    if pushed is not True:
        raise MeridianWorkflowError("review -> done requires pushed=true")


@dataclass
class TaskDocument:
    queue: str
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def task_id(self) -> str:
        raw = self.metadata.get("id") or self.path.stem
        return str(raw)

    @property
    def filename(self) -> str:
        return self.path.name

    def as_ref(self) -> "TaskRef":
        return TaskRef(
            queue=self.queue,
            path=self.path,
            task_id=self.task_id,
            filename=self.filename,
            branch=str(self.metadata.get("pr_branch") or self.metadata.get("branch") or "") or None,
            metadata=dict(self.metadata),
        )


@dataclass(frozen=True)
class TaskRef:
    queue: str
    path: Path
    task_id: str
    filename: str
    branch: str | None = None
    metadata: dict[str, Any] | None = None


def load_task_document(path: Path, queue: str) -> TaskDocument:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MeridianWorkflowError(f"Unable to read task file: {path}") from exc
    metadata, body = _split_task_document(content)
    metadata.setdefault("id", path.stem)
    metadata.setdefault("title", metadata["id"])
    metadata["status"] = queue
    return TaskDocument(queue=queue, path=path, metadata=metadata, body=body)


def list_task_refs(workspace: str | Path | None = None) -> dict[str, list[TaskRef]]:
    workspace_path = Path(workspace or ".").resolve()
    queues: dict[str, list[TaskRef]] = {}
    for queue in QUEUE_NAMES:
        refs: list[TaskRef] = []
        seen_keys: set[tuple[str, str]] = set()
        for directory in queue_dir_candidates(workspace_path, queue):
            if not directory.exists():
                continue
            for path in sorted(
                candidate
                for candidate in directory.iterdir()
                if candidate.is_file() and not candidate.name.startswith(".")
            ):
                document = load_task_document(path, queue)
                key = (document.task_id, document.filename)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                refs.append(document.as_ref())
        queues[queue] = refs
    return queues


def locate_task(workspace: str | Path | None, task_id: str) -> TaskDocument:
    workspace_path = Path(workspace or ".").resolve()
    matches: list[TaskDocument] = []
    preferred_match: TaskDocument | None = None
    for queue in QUEUE_NAMES:
        for index, directory in enumerate(queue_dir_candidates(workspace_path, queue)):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if not path.is_file() or path.name.startswith("."):
                    continue
                document = load_task_document(path, queue)
                if document.task_id != task_id and document.filename != task_id:
                    continue
                if index == 0:
                    preferred_match = document
                matches.append(document)
    if not matches:
        raise MeridianWorkflowError(f"Task not found: {task_id}")
    if preferred_match is not None:
        equivalent_matches = [
            match for match in matches
            if match.task_id == preferred_match.task_id and match.queue == preferred_match.queue
        ]
        if equivalent_matches:
            return preferred_match
    if len(matches) > 1:
        locations = ", ".join(str(match.path) for match in matches)
        raise MeridianWorkflowError(f"Task id is ambiguous across queues: {task_id} ({locations})")
    return matches[0]


def _validate_actor_for_queue(actor: str, queue: str, *, action: str) -> None:
    allowed = PERSONA_QUEUES.get(actor)
    if allowed is None or queue not in allowed:
        raise MeridianWorkflowError(f"{actor} cannot {action} tasks in {queue}")


def _merge_metadata_patch(metadata: dict[str, Any], metadata_patch: dict[str, Any] | None) -> None:
    if not metadata_patch:
        return
    for key, value in metadata_patch.items():
        if key in {"status", HISTORY_KEY}:
            continue
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value


def _persist_document(document: TaskDocument, destination_queue: str | None = None) -> TaskDocument:
    destination_queue = destination_queue or document.queue
    workspace = workspace_root_from_task_path(document.path)
    destination_path = canonical_queue_dir(workspace, destination_queue) / document.path.name
    document.metadata["status"] = destination_queue
    _clear_empty_fields(document.metadata)
    rendered = _render_task_document(document.metadata, document.body)

    if destination_path == document.path:
        _atomic_write_text(document.path, rendered)
        return TaskDocument(destination_queue, document.path, dict(document.metadata), document.body)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(destination_path.parent),
        prefix=f".{destination_path.stem}_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, destination_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if document.path.exists():
        document.path.unlink()
    return TaskDocument(destination_queue, destination_path, dict(document.metadata), document.body)


def claim_task(
    workspace: str | Path | None,
    *,
    task_id: str,
    actor: str,
    lease_ttl: int | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    actor = _normalize_actor(actor)
    timestamp = _isoformat(now or _utcnow())
    document = locate_task(workspace, task_id)
    if document.queue in TERMINAL_QUEUES:
        raise MeridianWorkflowError(f"Cannot claim a task in {document.queue}")

    claimed_by = document.metadata.get("claimed_by")
    if claimed_by and claimed_by != actor:
        raise MeridianWorkflowError(f"Task {document.task_id} is already claimed by {claimed_by}")
    if document.queue == "ready" and actor != "fatih":
        raise MeridianWorkflowError("Only fatih can claim tasks in ready")
    _validate_actor_for_queue(actor, document.queue, action="claim")

    claim_expires_at = None
    if lease_ttl is not None:
        if lease_ttl <= 0:
            raise MeridianWorkflowError("lease_ttl must be positive when provided")
        claim_expires_at = _isoformat((now or _utcnow()) + timedelta(seconds=lease_ttl))

    document.metadata["claimed_by"] = actor
    document.metadata.setdefault("assigned_to", actor)
    document.metadata["claimed_at"] = document.metadata.get("claimed_at") or timestamp
    document.metadata["claim_expires_at"] = claim_expires_at
    document.metadata["updated_at"] = timestamp
    document.metadata["last_transition_at"] = document.metadata.get("last_transition_at") or timestamp
    _append_history(
        document.metadata,
        {
            "event": "task_claimed",
            "actor": actor,
            "queue": document.queue,
            "at": timestamp,
            "reason": reason,
            "claim_expires_at": claim_expires_at,
        },
    )
    saved = _persist_document(document)
    emit_meridian_event(
        "task_claimed",
        {
            "workspace": str(Path(workspace or ".").resolve()),
            "task_id": saved.task_id,
            "queue": saved.queue,
            "actor": actor,
            "claim_expires_at": saved.metadata.get("claim_expires_at"),
            "reason": reason,
        },
        now=now or _utcnow(),
    )
    return {
        "task_id": saved.task_id,
        "queue": saved.queue,
        "claimed_by": saved.metadata.get("claimed_by"),
        "claimed_at": saved.metadata.get("claimed_at"),
        "claim_expires_at": saved.metadata.get("claim_expires_at"),
        "status": saved.metadata.get("status"),
        "path": str(saved.path),
    }


def _validate_transition(
    document: TaskDocument,
    *,
    actor: str,
    from_queue: str | None,
    to_queue: str,
    reason: str | None,
) -> None:
    if from_queue and from_queue != document.queue:
        raise MeridianWorkflowError(
            f"Task {document.task_id} is in {document.queue}, not {from_queue}"
        )
    if to_queue not in QUEUE_NAMES:
        raise MeridianWorkflowError(f"Unknown destination queue: {to_queue}")
    if to_queue == document.queue:
        raise MeridianWorkflowError(f"Task {document.task_id} is already in {to_queue}")
    allowed = TRANSITION_RULES.get(document.queue, frozenset())
    if to_queue not in allowed:
        raise MeridianWorkflowError(f"Invalid transition: {document.queue} -> {to_queue}")
    if (document.queue, to_queue) in EXCEPTIONAL_TRANSITIONS and not (reason or "").strip():
        raise MeridianWorkflowError(
            f"Transition {document.queue} -> {to_queue} requires a reason"
        )

    if (document.queue, to_queue) == ("ready", "in_progress"):
        if actor != "fatih":
            raise MeridianWorkflowError("Only fatih can transition ready -> in_progress")
        if document.metadata.get("claimed_by") != actor:
            raise MeridianWorkflowError(
                "ready -> in_progress requires the task to be claimed by the transitioning actor"
            )
        _validate_actor_for_queue(actor, "ready", action="transition")
    elif document.queue == "review":
        if to_queue == "done":
            _require_review_completion_metadata(document)
        _validate_actor_for_queue(actor, "review", action="transition")
    elif document.queue == "waiting_human":
        _validate_actor_for_queue(actor, "waiting_human", action="transition")
    elif document.queue in {"backlog", "debt", "in_progress"}:
        if (document.queue, to_queue) == ("in_progress", "review"):
            _require_review_handoff_metadata(document)
        _validate_actor_for_queue(actor, document.queue, action="transition")


def transition_task(
    workspace: str | Path | None,
    *,
    task_id: str,
    actor: str,
    to_queue: str,
    from_queue: str | None = None,
    reason: str | None = None,
    notes: str | None = None,
    metadata_patch: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    actor = _normalize_actor(actor)
    document = locate_task(workspace, task_id)
    _validate_transition(
        document,
        actor=actor,
        from_queue=from_queue,
        to_queue=to_queue,
        reason=reason,
    )

    timestamp = _isoformat(now or _utcnow())
    previous_queue = document.queue
    _merge_metadata_patch(document.metadata, metadata_patch)
    document.metadata["status"] = to_queue
    document.metadata["updated_at"] = timestamp
    document.metadata["last_transition_at"] = timestamp

    if to_queue == "waiting_human":
        document.metadata["waiting_on"] = "human_confirmation"
        document.metadata["blocked_reason"] = reason or document.metadata.get("blocked_reason")
    elif to_queue == "done":
        document.metadata.pop("waiting_on", None)
        document.metadata.pop("blocked_reason", None)
        document.metadata.pop("claimed_by", None)
        document.metadata.pop("claimed_at", None)
        document.metadata.pop("claim_expires_at", None)
    elif to_queue == "backlog":
        document.metadata.pop("claimed_by", None)
        document.metadata.pop("claimed_at", None)
        document.metadata.pop("claim_expires_at", None)
        document.metadata.pop("waiting_on", None)
    elif to_queue == "review":
        document.metadata["reviewer"] = "matthew"
        document.metadata.pop("waiting_on", None)
    elif to_queue == "in_progress":
        document.metadata.pop("waiting_on", None)
        document.metadata.setdefault("assigned_to", "fatih")

    _append_history(
        document.metadata,
        {
            "event": "task_transitioned",
            "actor": actor,
            "from_queue": previous_queue,
            "to_queue": to_queue,
            "at": timestamp,
            "reason": reason,
            "notes": notes,
        },
    )
    saved = _persist_document(document, destination_queue=to_queue)
    emit_meridian_event(
        "task_transitioned",
        {
            "workspace": str(Path(workspace or ".").resolve()),
            "task_id": saved.task_id,
            "actor": actor,
            "from_queue": previous_queue,
            "to_queue": saved.queue,
            "reason": reason,
            "notes": notes,
        },
        now=now or _utcnow(),
    )
    return {
        "task_id": saved.task_id,
        "from_queue": previous_queue,
        "to_queue": saved.queue,
        "status": saved.metadata.get("status"),
        "claimed_by": saved.metadata.get("claimed_by"),
        "waiting_on": saved.metadata.get("waiting_on"),
        "blocked_reason": saved.metadata.get("blocked_reason"),
        "last_transition_at": saved.metadata.get("last_transition_at"),
        "path": str(saved.path),
    }
