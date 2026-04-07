"""Meridian task-change notifier helpers."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


QUEUE_ORDER = ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt")
STATE_PATH = get_hermes_home() / "meridian" / "notifier_state.json"


@dataclass(frozen=True)
class TaskSnapshot:
    queue: str
    filename: str


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


def _local_snapshot(workspace: Path) -> dict[str, TaskSnapshot]:
    snapshot: dict[str, TaskSnapshot] = {}
    for queue in QUEUE_ORDER:
        queue_dir = workspace / "tasks" / queue
        if not queue_dir.exists():
            continue
        for path in sorted(queue_dir.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            snapshot[path.name] = TaskSnapshot(queue=queue, filename=path.name)
    return snapshot


def _remote_snapshot(settings: dict[str, str]) -> dict[str, TaskSnapshot]:
    script = """
import json
from pathlib import Path
workspace = Path(%r).expanduser()
queues = %r
payload = {}
for queue in queues:
    queue_dir = workspace / "tasks" / queue
    if not queue_dir.exists():
        continue
    for path in sorted(queue_dir.iterdir()):
        if not path.is_file() or path.name.startswith('.'):
            continue
        payload[path.name] = {"queue": queue, "filename": path.name}
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


def collect_snapshot() -> dict[str, TaskSnapshot]:
    _load_dotenv(Path.home() / ".hermes" / ".env")
    ssh = _ssh_env()
    if ssh:
        return _remote_snapshot(ssh)
    workspace = Path(os.getenv("HERMES_MERIDIAN_WORKSPACE", ".")).expanduser()
    return _local_snapshot(workspace)


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
    previous = load_state(state_path)
    current = collect_snapshot()
    save_state(current, state_path)
    previous_tasks = waiting_human_tasks(previous)
    current_tasks = waiting_human_tasks(current)
    return {
        "tasks": current_tasks,
        "brief": format_waiting_human_brief(current_tasks),
        "changed": current_tasks != previous_tasks,
        "has_waiting_human": bool(current_tasks),
    }
