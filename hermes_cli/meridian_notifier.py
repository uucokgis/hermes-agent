"""Meridian task-change notifier helpers."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli.meridian_workflow import QUEUE_NAMES, queue_dir_candidates


QUEUE_ORDER = QUEUE_NAMES
STATE_PATH = get_hermes_home() / "meridian" / "notifier_state.json"
SUPPORT_QUEUES = ("inbox", "responded", "summaries")


@dataclass(frozen=True)
class TaskSnapshot:
    queue: str
    filename: str


@dataclass(frozen=True)
class SupportTicketSnapshot:
    ticket_id: str
    queue: str
    summary: str
    status: str
    updated_at: str
    last_human_reply_at: str


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _ssh_env() -> dict[str, str] | None:
    host = os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_HOST", "").strip()
    user = os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_USER", "").strip()
    key = os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_KEY", "").strip()
    password = os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_PASSWORD", "").strip()
    workspace = os.getenv("HERMES_MERIDIAN_NOTIFY_WORKSPACE", "").strip() or "/home/umut/meridian"
    if not host or not user:
        return None
    return {
        "host": host,
        "user": user,
        "key": key,
        "password": password,
        "workspace": workspace,
    }


def _state_payload(snapshot: dict[str, TaskSnapshot]) -> dict[str, Any]:
    return {
        "tasks": {
            name: {"queue": item.queue, "filename": item.filename}
            for name, item in sorted(snapshot.items())
        }
    }


def _combined_state_payload(
    task_snapshot: dict[str, TaskSnapshot],
    support_snapshot: dict[str, SupportTicketSnapshot],
) -> dict[str, Any]:
    return {
        **_state_payload(task_snapshot),
        "support_tickets": {
            ticket_id: {
                "queue": item.queue,
                "summary": item.summary,
                "status": item.status,
                "updated_at": item.updated_at,
                "last_human_reply_at": item.last_human_reply_at,
            }
            for ticket_id, item in sorted(support_snapshot.items())
        },
    }


def load_state(path: Path = STATE_PATH) -> dict[str, TaskSnapshot]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    tasks = payload.get("tasks") if isinstance(payload, dict) else {}
    if not isinstance(tasks, dict):
        return {}
    result: dict[str, TaskSnapshot] = {}
    for name, item in tasks.items():
        if not isinstance(item, dict):
            continue
        queue = str(item.get("queue") or "").strip()
        filename = str(item.get("filename") or name).strip()
        if queue:
            result[str(name)] = TaskSnapshot(queue=queue, filename=filename)
    return result


def save_state(snapshot: dict[str, TaskSnapshot], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_state_payload(snapshot), ensure_ascii=False, indent=2), encoding="utf-8")


def load_support_state(path: Path = STATE_PATH) -> dict[str, SupportTicketSnapshot]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    tickets = payload.get("support_tickets") if isinstance(payload, dict) else {}
    if not isinstance(tickets, dict):
        return {}
    result: dict[str, SupportTicketSnapshot] = {}
    for ticket_id, item in tickets.items():
        if not isinstance(item, dict):
            continue
        result[str(ticket_id)] = SupportTicketSnapshot(
            ticket_id=str(ticket_id),
            queue=str(item.get("queue") or "inbox"),
            summary=str(item.get("summary") or ticket_id),
            status=str(item.get("status") or ""),
            updated_at=str(item.get("updated_at") or ""),
            last_human_reply_at=str(item.get("last_human_reply_at") or ""),
        )
    return result


def save_combined_state(
    task_snapshot: dict[str, TaskSnapshot],
    support_snapshot: dict[str, SupportTicketSnapshot],
    path: Path = STATE_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_combined_state_payload(task_snapshot, support_snapshot), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _local_snapshot(workspace: Path) -> dict[str, TaskSnapshot]:
    snapshot: dict[str, TaskSnapshot] = {}
    for queue in QUEUE_ORDER:
        for queue_dir in queue_dir_candidates(workspace, queue):
            if not queue_dir.exists():
                continue
            for path in sorted(queue_dir.iterdir()):
                if not path.is_file() or path.name.startswith("."):
                    continue
                snapshot.setdefault(path.name, TaskSnapshot(queue=queue, filename=path.name))
    return snapshot


def _remote_snapshot(settings: dict[str, str]) -> dict[str, TaskSnapshot]:
    script = """
import json
from pathlib import Path
workspace = Path(%r).expanduser()
queues = %r
aliases = {"in_progress": ("in-progress",)}
payload = {}
for queue in queues:
    for name in (queue, *aliases.get(queue, ())):
        queue_dir = workspace / "tasks" / name
        if not queue_dir.exists():
            continue
        for path in sorted(queue_dir.iterdir()):
            if not path.is_file() or path.name.startswith('.'):
                continue
            payload.setdefault(path.name, {"queue": queue, "filename": path.name})
print(json.dumps(payload, ensure_ascii=False))
""" % (settings["workspace"], QUEUE_ORDER)
    if settings.get("password"):
        cmd = [
            "sshpass",
            "-e",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "ConnectTimeout=8",
            f"{settings['user']}@{settings['host']}",
            f"python3 - <<'PY'\n{script}\nPY",
        ]
    else:
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=8",
        ]
        if settings.get("key"):
            cmd.extend(["-i", settings["key"]])
        cmd.extend(
            [
                f"{settings['user']}@{settings['host']}",
                f"python3 - <<'PY'\n{script}\nPY",
            ]
        )
    env = os.environ.copy()
    if settings.get("password"):
        env["SSHPASS"] = settings["password"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "remote snapshot failed")
    payload = json.loads(result.stdout.strip() or "{}")
    return {
        name: TaskSnapshot(queue=str(item["queue"]), filename=str(item.get("filename") or name))
        for name, item in payload.items()
        if isinstance(item, dict) and item.get("queue")
    }


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_ticket_file(path: Path, queue: str) -> SupportTicketSnapshot | None:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if content.startswith("---\n"):
        closing = content.find("\n---\n", 4)
        if closing != -1:
            import yaml

            raw = content[4:closing]
            metadata = yaml.safe_load(raw) or {}
        else:
            metadata = {}
    else:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    ticket_id = str(metadata.get("ticket_id") or path.stem).strip()
    return SupportTicketSnapshot(
        ticket_id=ticket_id,
        queue=queue,
        summary=str(metadata.get("summary") or ticket_id),
        status=str(metadata.get("status") or queue),
        updated_at=str(metadata.get("updated_at") or ""),
        last_human_reply_at=str(metadata.get("last_human_reply_at") or ""),
    )


def _local_support_snapshot(workspace: Path) -> dict[str, SupportTicketSnapshot]:
    support_root = workspace / "customer_support"
    snapshot: dict[str, SupportTicketSnapshot] = {}
    for queue in SUPPORT_QUEUES:
        queue_dir = support_root / queue
        if not queue_dir.exists():
            continue
        for path in sorted(queue_dir.glob("TICKET-*.md")):
            item = _load_ticket_file(path, queue)
            if item:
                snapshot[item.ticket_id] = item
    return snapshot


def _remote_support_snapshot(settings: dict[str, str]) -> dict[str, SupportTicketSnapshot]:
    script = """
import json
from pathlib import Path
import yaml
workspace = Path(%r).expanduser()
queues = %r
payload = {}
support_root = workspace / "customer_support"
for queue in queues:
    queue_dir = support_root / queue
    if not queue_dir.exists():
        continue
    for path in sorted(queue_dir.glob("TICKET-*.md")):
        content = path.read_text(encoding="utf-8")
        metadata = {}
        if content.startswith("---\\n"):
            closing = content.find("\\n---\\n", 4)
            if closing != -1:
                raw = content[4:closing]
                loaded = yaml.safe_load(raw) or {}
                if isinstance(loaded, dict):
                    metadata = loaded
        ticket_id = str(metadata.get("ticket_id") or path.stem).strip()
        payload[ticket_id] = {
            "queue": queue,
            "summary": str(metadata.get("summary") or ticket_id),
            "status": str(metadata.get("status") or queue),
            "updated_at": str(metadata.get("updated_at") or ""),
            "last_human_reply_at": str(metadata.get("last_human_reply_at") or ""),
        }
print(json.dumps(payload, ensure_ascii=False))
""" % (settings["workspace"], SUPPORT_QUEUES)
    if settings.get("password"):
        cmd = [
            "sshpass",
            "-e",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "ConnectTimeout=8",
            f"{settings['user']}@{settings['host']}",
            f"python3 - <<'PY'\n{script}\nPY",
        ]
    else:
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=8",
        ]
        if settings.get("key"):
            cmd.extend(["-i", settings["key"]])
        cmd.extend(
            [
                f"{settings['user']}@{settings['host']}",
                f"python3 - <<'PY'\n{script}\nPY",
            ]
        )
    env = os.environ.copy()
    if settings.get("password"):
        env["SSHPASS"] = settings["password"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "remote support snapshot failed")
    payload = json.loads(result.stdout.strip() or "{}")
    return {
        ticket_id: SupportTicketSnapshot(
            ticket_id=str(ticket_id),
            queue=str(item.get("queue") or "inbox"),
            summary=str(item.get("summary") or ticket_id),
            status=str(item.get("status") or ""),
            updated_at=str(item.get("updated_at") or ""),
            last_human_reply_at=str(item.get("last_human_reply_at") or ""),
        )
        for ticket_id, item in payload.items()
        if isinstance(item, dict)
    }


def collect_snapshot() -> dict[str, TaskSnapshot]:
    _load_dotenv(Path.home() / ".hermes" / ".env")
    ssh = _ssh_env()
    if ssh:
        return _remote_snapshot(ssh)
    workspace = Path(os.getenv("HERMES_MERIDIAN_WORKSPACE", ".")).expanduser()
    return _local_snapshot(workspace)


def collect_support_snapshot() -> dict[str, SupportTicketSnapshot]:
    _load_dotenv(Path.home() / ".hermes" / ".env")
    ssh = _ssh_env()
    if ssh:
        return _remote_support_snapshot(ssh)
    workspace = Path(os.getenv("HERMES_MERIDIAN_WORKSPACE", ".")).expanduser()
    return _local_support_snapshot(workspace)


def _transition_label(old_queue: str | None, new_queue: str | None) -> str:
    if old_queue is None and new_queue == "ready":
        return "ready"
    if old_queue is None and new_queue == "in_progress":
        return "started"
    if new_queue == "done":
        return "done"
    if new_queue == "review":
        return "review"
    if new_queue == "in_progress":
        return "started"
    if new_queue == "waiting_human":
        return "needs_input"
    if new_queue == "ready":
        return "ready"
    if new_queue == "debt":
        return "debt"
    return "changed"


def build_changes(previous: dict[str, TaskSnapshot], current: dict[str, TaskSnapshot]) -> list[dict[str, str]]:
    names = sorted(set(previous) | set(current))
    changes: list[dict[str, str]] = []
    for name in names:
        old = previous.get(name)
        new = current.get(name)
        if old and new and old.queue == new.queue:
            continue
        if new is None:
            changes.append({"task": name, "kind": "removed", "from": old.queue if old else "-", "to": "-"})
            continue
        changes.append(
            {
                "task": name,
                "kind": _transition_label(old.queue if old else None, new.queue),
                "from": old.queue if old else "-",
                "to": new.queue,
            }
        )
    priority = {"needs_input": 0, "done": 1, "review": 2, "started": 3, "ready": 4, "debt": 5, "changed": 6, "removed": 7}
    return sorted(changes, key=lambda item: (priority.get(item["kind"], 9), item["task"]))


def format_brief(changes: list[dict[str, str]], *, max_items: int = 6) -> str:
    if not changes:
        return ""
    kind_map = {
        "done": "done",
        "review": "review",
        "started": "started",
        "ready": "ready",
        "needs_input": "needs input",
        "debt": "debt",
        "changed": "changed",
        "removed": "removed",
    }
    lines = []
    for item in changes[:max_items]:
        label = kind_map.get(item["kind"], item["kind"])
        lines.append(f"- {label}: {item['task']} ({item['from']} -> {item['to']})")
    remaining = len(changes) - max_items
    if remaining > 0:
        lines.append(f"- +{remaining} more change(s)")
    return "\n".join(lines)


def waiting_human_tasks(snapshot: dict[str, TaskSnapshot]) -> list[str]:
    return sorted(item.filename for item in snapshot.values() if item.queue == "waiting_human")


def format_waiting_human_brief(tasks: list[str], *, max_items: int = 5) -> str:
    if not tasks:
        return ""
    lines = [f"waiting_human: {len(tasks)} task(s) still need Umut"]
    for name in tasks[:max_items]:
        lines.append(f"- {name}")
    remaining = len(tasks) - max_items
    if remaining > 0:
        lines.append(f"- +{remaining} more waiting_human task(s)")
    return "\n".join(lines)


def tickets_needing_human(snapshot: dict[str, SupportTicketSnapshot]) -> list[SupportTicketSnapshot]:
    result: list[SupportTicketSnapshot] = []
    for item in snapshot.values():
        updated_at = _parse_iso(item.updated_at)
        last_human_reply_at = _parse_iso(item.last_human_reply_at)
        # New agent-side activity after the last human reply means the thread is active again.
        if updated_at and (last_human_reply_at is None or updated_at > last_human_reply_at):
            result.append(item)
    return sorted(result, key=lambda item: (_parse_iso(item.updated_at) or datetime.min.replace(tzinfo=timezone.utc), item.ticket_id), reverse=True)


def format_support_brief(tickets: list[SupportTicketSnapshot], *, max_items: int = 4) -> str:
    if not tickets:
        return ""
    lines = [f"support follow-up: {len(tickets)} ticket(s) waiting on you"]
    for item in tickets[:max_items]:
        lines.append(f"- {item.ticket_id} [{item.status or item.queue}] {item.summary}")
    remaining = len(tickets) - max_items
    if remaining > 0:
        lines.append(f"- +{remaining} more support ticket(s)")
    return "\n".join(lines)


def run_notifier(*, state_path: Path = STATE_PATH) -> dict[str, Any]:
    previous = load_state(state_path)
    current = collect_snapshot()
    changes = build_changes(previous, current)
    save_state(current, state_path)
    return {
        "changes": changes,
        "brief": format_brief(changes),
        "changed": bool(changes),
    }


def run_waiting_human_notifier(*, state_path: Path = STATE_PATH) -> dict[str, Any]:
    previous_tasks_state = load_state(state_path)
    previous_support_state = load_support_state(state_path)
    current_tasks_state = collect_snapshot()
    current_support_state = collect_support_snapshot()
    save_combined_state(current_tasks_state, current_support_state, state_path)
    previous_tasks = waiting_human_tasks(previous_tasks_state)
    current_tasks = waiting_human_tasks(current_tasks_state)
    previous_support = [item.ticket_id for item in tickets_needing_human(previous_support_state)]
    current_support = tickets_needing_human(current_support_state)
    current_support_ids = [item.ticket_id for item in current_support]
    parts = [part for part in (format_waiting_human_brief(current_tasks), format_support_brief(current_support)) if part]
    return {
        "tasks": current_tasks,
        "support_ticket_ids": current_support_ids,
        "brief": "\n\n".join(parts),
        "changed": current_tasks != previous_tasks or current_support_ids != previous_support,
        "has_waiting_human": bool(current_tasks),
        "has_support_waiting": bool(current_support_ids),
    }
