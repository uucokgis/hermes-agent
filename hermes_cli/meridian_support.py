"""Meridian customer-support tickets and role status helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_cli.config import load_config
from hermes_cli.meridian_dispatcher import _resolve_workspace_path


ROLE_NAMES = ("philip", "fatih", "matthew")
SUPPORT_QUEUES = ("inbox", "responded", "summaries")
HEADER_RE = re.compile(r"^=== (?P<timestamp>\S+) \[(?P<role>[a-z]+)\] profile=(?P<profile>\S+) workspace=(?P<workspace>.+) ===$")
MERIDIAN_QUEUE_NAMES = ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt")
FOCUS_TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Drawing widget", ("drawing", "geometry", "edit-operations", "editor")),
    ("Attribute table", ("attribute", "table")),
    ("Routing / CSV", ("route", "routing", "csv", "upload")),
    ("Layer visibility", ("layer", "visibility")),
)


@dataclass(frozen=True)
class MeridianTicket:
    ticket_id: str
    path: Path
    queue: str
    metadata: dict[str, Any]
    body: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def resolve_support_workspace(workspace: str | Path | None = None) -> Path:
    return _resolve_workspace_path(workspace)


def support_root(workspace: str | Path | None = None) -> Path:
    return resolve_support_workspace(workspace) / "customer_support"


def ensure_support_dirs(workspace: str | Path | None = None) -> Path:
    root = support_root(workspace)
    for name in SUPPORT_QUEUES:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if content.startswith("---\n"):
        closing = content.find("\n---\n", 4)
        if closing != -1:
            raw = content[4:closing]
            body = content[closing + 5 :]
            parsed = yaml.safe_load(raw) or {}
            return dict(parsed) if isinstance(parsed, dict) else {}, body.lstrip("\n")
    return {}, content


def _render_ticket(metadata: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    body = body.strip("\n")
    parts = ["---", frontmatter, "---"]
    if body:
        parts.extend(["", body])
    return "\n".join(parts) + "\n"


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "ticket"


def _ticket_filename(ticket_id: str, summary: str) -> str:
    return f"TICKET-{ticket_id}-{_slugify(summary)[:60]}.md"


def _next_ticket_id(root: Path, *, now: datetime | None = None) -> str:
    current = now or _utcnow()
    prefix = current.strftime("%Y%m%d")
    existing: set[int] = set()
    for queue in SUPPORT_QUEUES:
        queue_dir = root / queue
        if not queue_dir.exists():
            continue
        for path in queue_dir.glob("TICKET-*.md"):
            match = re.match(r"TICKET-(\d+)-", path.name)
            if not match:
                continue
            ticket_id = match.group(1)
            if ticket_id.startswith(prefix):
                try:
                    existing.add(int(ticket_id[len(prefix):]))
                except ValueError:
                    continue
    next_seq = (max(existing) + 1) if existing else 1
    return f"{prefix}{next_seq:03d}"


def _human_update_block(author: str, message: str, *, timestamp: str) -> str:
    return (
        "## Human Update\n\n"
        f"- at: {timestamp}\n"
        f"- from: {author}\n"
        f"- message: {message.strip()}\n"
    )


def create_support_ticket(
    *,
    summary: str,
    message: str,
    target_role: str | None = None,
    workspace: str | Path | None = None,
    source: str = "telegram",
    sender: str = "",
    now: datetime | None = None,
) -> MeridianTicket:
    root = ensure_support_dirs(workspace)
    current = now or _utcnow()
    role = (target_role or "").strip().lower() or "philip"
    if role not in ROLE_NAMES:
        role = "philip"
    ticket_id = _next_ticket_id(root, now=current)
    metadata = {
        "ticket_id": ticket_id,
        "summary": summary.strip(),
        "source": source,
        "sender": sender.strip(),
        "target_role": role,
        "status": "pending_role",
        "created_at": _isoformat(current),
        "updated_at": _isoformat(current),
        "last_human_reply_at": _isoformat(current),
    }
    body = (
        "# Request\n\n"
        f"{message.strip()}\n\n"
        "# Role Notes\n\n"
        "Pending.\n\n"
        "# Human Updates\n\n"
        f"{_human_update_block(sender or source, message, timestamp=_isoformat(current))}"
    )
    path = root / "inbox" / _ticket_filename(ticket_id, summary)
    path.write_text(_render_ticket(metadata, body), encoding="utf-8")
    return MeridianTicket(ticket_id=ticket_id, path=path, queue="inbox", metadata=metadata, body=body)


def list_support_tickets(
    workspace: str | Path | None = None,
    *,
    limit: int = 10,
) -> list[MeridianTicket]:
    root = ensure_support_dirs(workspace)
    found: list[MeridianTicket] = []
    for queue in SUPPORT_QUEUES:
        queue_dir = root / queue
        for path in sorted(queue_dir.glob("TICKET-*.md")):
            metadata, body = _split_frontmatter(path.read_text(encoding="utf-8"))
            ticket_id = str(metadata.get("ticket_id") or "")
            if not ticket_id:
                match = re.match(r"TICKET-(\d+)-", path.name)
                ticket_id = match.group(1) if match else path.stem
            found.append(MeridianTicket(ticket_id=ticket_id, path=path, queue=queue, metadata=metadata, body=body))

    def _sort_key(ticket: MeridianTicket) -> tuple[str, str]:
        updated = str(ticket.metadata.get("updated_at") or "")
        return (updated, ticket.ticket_id)

    return sorted(found, key=_sort_key, reverse=True)[: max(limit, 1)]


def get_support_ticket(ticket_id: str, workspace: str | Path | None = None) -> MeridianTicket | None:
    normalized = str(ticket_id).strip()
    if not normalized:
        return None
    root = ensure_support_dirs(workspace)
    for queue in SUPPORT_QUEUES:
        queue_dir = root / queue
        for path in queue_dir.glob(f"TICKET-{normalized}-*.md"):
            metadata, body = _split_frontmatter(path.read_text(encoding="utf-8"))
            return MeridianTicket(ticket_id=normalized, path=path, queue=queue, metadata=metadata, body=body)
    return None


def append_human_reply(
    ticket_id: str,
    *,
    message: str,
    sender: str,
    workspace: str | Path | None = None,
    now: datetime | None = None,
) -> MeridianTicket:
    ticket = get_support_ticket(ticket_id, workspace)
    if ticket is None:
        raise FileNotFoundError(f"Ticket not found: {ticket_id}")
    current = now or _utcnow()
    metadata = dict(ticket.metadata)
    metadata["updated_at"] = _isoformat(current)
    metadata["last_human_reply_at"] = _isoformat(current)
    metadata["status"] = "human_replied"
    body = ticket.body.rstrip() + "\n\n" + _human_update_block(sender, message, timestamp=_isoformat(current))
    ticket.path.write_text(_render_ticket(metadata, body), encoding="utf-8")
    return MeridianTicket(ticket_id=ticket.ticket_id, path=ticket.path, queue=ticket.queue, metadata=metadata, body=body)


def format_ticket_summary(ticket: MeridianTicket) -> str:
    summary = str(ticket.metadata.get("summary") or ticket.ticket_id)
    role = str(ticket.metadata.get("target_role") or "philip")
    status = str(ticket.metadata.get("status") or ticket.queue)
    updated = str(ticket.metadata.get("updated_at") or "")
    return f"`{ticket.ticket_id}` [{role}] {summary} ({status}, {updated[:16].replace('T', ' ')})"


def format_ticket_detail(ticket: MeridianTicket) -> str:
    metadata = ticket.metadata
    lines = [
        f"🎫 **Meridian Ticket `{ticket.ticket_id}`**",
        "",
        f"**Summary:** {metadata.get('summary', '')}",
        f"**Target Role:** `{metadata.get('target_role', 'philip')}`",
        f"**Status:** `{metadata.get('status', ticket.queue)}`",
        f"**Queue:** `{ticket.queue}`",
        f"**Updated:** {str(metadata.get('updated_at', ''))[:19].replace('T', ' ')}",
        "",
        ticket.body[:1500].strip() or "_No body_",
    ]
    return "\n".join(lines)


def role_loop_state(role: str) -> dict[str, Any]:
    role_name = role.strip().lower()
    state_dir = Path.home() / ".hermes" / "meridian" / "loops"
    log_path = state_dir / f"{role_name}.loop.log"
    pid_path = state_dir / f"{role_name}.loop.pid"
    pid = None
    running = False
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            running = True
        except Exception:
            running = False

    summary = ""
    header = ""
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = 0
        for idx, line in enumerate(lines):
            if f"[{role_name}] profile=" in line:
                start = idx
                header = line
        segment = lines[start:] if lines else []
        summary_lines: list[str] = []
        for raw in segment[1:]:
            line = raw.strip(" \t│╭╰─")
            if not line:
                continue
            if line.startswith(("┊", "a//", "@@", "[tool]")):
                continue
            if "preparing " in line or line.startswith("$"):
                continue
            if line.startswith("==="):
                break
            if any(token in line for token in ("Hermes", "review diff")):
                continue
            summary_lines.append(line)
            if len(summary_lines) >= 4:
                break
        summary = " ".join(summary_lines).strip()

    return {
        "role": role_name,
        "running": running,
        "pid": pid,
        "header": header,
        "summary": summary or "No recent summary yet.",
        "log_path": str(log_path),
    }


def _ssh_terminal_settings() -> dict[str, str] | None:
    try:
        config = load_config()
    except Exception:
        return None
    terminal = config.get("terminal") or {}
    if str(terminal.get("backend") or "").strip().lower() != "ssh":
        return None

    host = (os.getenv("TERMINAL_SSH_HOST") or terminal.get("ssh_host") or "").strip()
    user = (os.getenv("TERMINAL_SSH_USER") or terminal.get("ssh_user") or "").strip()
    key = (os.getenv("TERMINAL_SSH_KEY") or terminal.get("ssh_key") or "").strip()
    cwd = (
        os.getenv("HERMES_MERIDIAN_WORKSPACE")
        or terminal.get("cwd")
        or ""
    ).strip()
    if not host or not user:
        return None
    return {
        "host": host,
        "user": user,
        "key": key,
        "cwd": cwd or "~",
    }


def _collect_focus_matches(queue_map: dict[str, list[str]]) -> list[dict[str, str]]:
    focus_items: list[dict[str, str]] = []
    lower_map = {
        queue: [(name, name.lower()) for name in names]
        for queue, names in queue_map.items()
    }
    for label, keywords in FOCUS_TOPICS:
        matches: list[str] = []
        seen: set[str] = set()
        for queue in ("review", "in_progress", "ready", "backlog", "done", "debt"):
            for raw_name, lowered in lower_map.get(queue, []):
                if not any(keyword in lowered for keyword in keywords):
                    continue
                entry = f"{queue}: {raw_name}"
                if entry in seen:
                    continue
                seen.add(entry)
                matches.append(entry)
                if len(matches) >= 3:
                    break
            if len(matches) >= 3:
                break
        if matches:
            focus_items.append({"label": label, "items": " | ".join(matches)})
    return focus_items


def _git_headlines(workspace: Path, *, limit: int = 4) -> list[str]:
    if not (workspace / ".git").exists():
        return []
    try:
        result = subprocess.run(
            ["git", "log", f"-n{limit}", "--pretty=format:%h %s"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _recent_queue_items(workspace: Path, queue: str, *, limit: int = 3) -> list[str]:
    queue_dir = workspace / "tasks" / queue
    if not queue_dir.exists():
        return []
    items = [
        path
        for path in queue_dir.iterdir()
        if path.is_file() and not path.name.startswith(".")
    ]
    items.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [path.name for path in items[:limit]]


def _collect_local_workspace_summary(workspace: Path) -> dict[str, Any]:
    queue_map = {
        queue: sorted(
            [
                path.name
                for path in (workspace / "tasks" / queue).iterdir()
                if path.is_file() and not path.name.startswith(".")
            ]
        )
        if (workspace / "tasks" / queue).exists()
        else []
        for queue in MERIDIAN_QUEUE_NAMES
    }
    return {
        "workspace": str(workspace),
        "source": "local",
        "queue_counts": {queue: len(names) for queue, names in queue_map.items()},
        "recent_done": _recent_queue_items(workspace, "done"),
        "recent_review": _recent_queue_items(workspace, "review"),
        "recent_in_progress": _recent_queue_items(workspace, "in_progress"),
        "focus_items": _collect_focus_matches(queue_map),
        "recent_commits": _git_headlines(workspace),
    }


def _collect_remote_workspace_summary(settings: dict[str, str]) -> dict[str, Any] | None:
    script = r"""
import json
import subprocess
from pathlib import Path

workspace = Path("__WORKSPACE__").expanduser()
queues = ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt")
focus_topics = (
    ("Drawing widget", ("drawing", "geometry", "edit-operations", "editor")),
    ("Attribute table", ("attribute", "table")),
    ("Routing / CSV", ("route", "routing", "csv", "upload")),
    ("Layer visibility", ("layer", "visibility")),
)

def queue_items(queue):
    queue_dir = workspace / "tasks" / queue
    if not queue_dir.exists():
        return []
    return [p for p in queue_dir.iterdir() if p.is_file() and not p.name.startswith(".")]

queue_map = {
    queue: sorted(path.name for path in queue_items(queue))
    for queue in queues
}

def recent(queue, limit=3):
    items = queue_items(queue)
    items.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [path.name for path in items[:limit]]

def focus_matches():
    lowered = {
        queue: [(name, name.lower()) for name in names]
        for queue, names in queue_map.items()
    }
    result = []
    for label, keywords in focus_topics:
        matches = []
        seen = set()
        for queue in ("review", "in_progress", "ready", "backlog", "done", "debt"):
            for raw_name, lowered_name in lowered.get(queue, []):
                if not any(keyword in lowered_name for keyword in keywords):
                    continue
                entry = f"{queue}: {raw_name}"
                if entry in seen:
                    continue
                seen.add(entry)
                matches.append(entry)
                if len(matches) >= 3:
                    break
            if len(matches) >= 3:
                break
        if matches:
            result.append({"label": label, "items": " | ".join(matches)})
    return result

recent_commits = []
if (workspace / ".git").exists():
    try:
        result = subprocess.run(
            ["git", "log", "-n4", "--pretty=format:%h %s"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode == 0:
            recent_commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        recent_commits = []

print(json.dumps({
    "workspace": str(workspace),
    "source": "ssh",
    "queue_counts": {queue: len(names) for queue, names in queue_map.items()},
    "recent_done": recent("done"),
    "recent_review": recent("review"),
    "recent_in_progress": recent("in_progress"),
    "focus_items": focus_matches(),
    "recent_commits": recent_commits,
}, ensure_ascii=False))
""".replace("__WORKSPACE__", settings["cwd"].replace("\\", "\\\\").replace('"', '\\"'))

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
    cmd.append(f"{settings['user']}@{settings['host']}")
    cmd.append(f"python3 - <<'PY'\n{script}\nPY")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        payload = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["remote_host"] = settings["host"]
    payload["remote_user"] = settings["user"]
    return payload


def _load_workspace_summary(workspace: str | Path | None = None) -> dict[str, Any] | None:
    try:
        workspace_path = resolve_support_workspace(workspace)
    except Exception:
        workspace_path = None
    if workspace_path is not None and (workspace_path / "tasks").is_dir():
        return _collect_local_workspace_summary(workspace_path)

    settings = _ssh_terminal_settings()
    if settings:
        return _collect_remote_workspace_summary(settings)
    return None


def build_roles_status_text(workspace: str | Path | None = None) -> str:
    lines = ["🧭 **Meridian Role Status**", ""]
    try:
        from hermes_cli.meridian_quality import format_quality_status

        quality_status = format_quality_status()
    except Exception:
        quality_status = ""
    summary = _load_workspace_summary(workspace)
    if summary:
        queue_counts = summary.get("queue_counts") or {}
        source = summary.get("source") or "local"
        if source == "ssh":
            source_label = (
                f"{summary.get('remote_user', 'user')}@"
                f"{summary.get('remote_host', 'host')}:{summary.get('workspace', '')}"
            )
        else:
            source_label = str(summary.get("workspace") or "")
        lines.append(f"**Workspace:** `{source_label}` ({source})")
        lines.append(
            "**Queues:** "
            f"`backlog={queue_counts.get('backlog', 0)}` "
            f"`ready={queue_counts.get('ready', 0)}` "
            f"`in_progress={queue_counts.get('in_progress', 0)}` "
            f"`review={queue_counts.get('review', 0)}` "
            f"`done={queue_counts.get('done', 0)}` "
            f"`debt={queue_counts.get('debt', 0)}`"
        )
        recent_done = summary.get("recent_done") or []
        if recent_done:
            lines.append("**Recently Done:**")
            lines.extend(f"- `{item}`" for item in recent_done)
        recent_review = summary.get("recent_review") or []
        if recent_review:
            lines.append("**Needs Review:**")
            lines.extend(f"- `{item}`" for item in recent_review)
        recent_in_progress = summary.get("recent_in_progress") or []
        if recent_in_progress:
            lines.append("**In Progress:**")
            lines.extend(f"- `{item}`" for item in recent_in_progress)
        focus_items = summary.get("focus_items") or []
        if focus_items:
            lines.append("**Tracked Topics:**")
            for item in focus_items:
                lines.append(f"- **{item['label']}:** {item['items']}")
        recent_commits = summary.get("recent_commits") or []
        if recent_commits:
            lines.append("**Recent Commits:**")
            lines.extend(f"- `{item}`" for item in recent_commits[:4])
        if quality_status and not quality_status.startswith("No quality-gate"):
            quality_lines = [line.rstrip() for line in quality_status.splitlines()]
            lines.append("**Quality Gate:**")
            if len(quality_lines) > 1:
                lines.append(f"- {quality_lines[1].strip()}")
            if len(quality_lines) > 2:
                lines.append(f"- {quality_lines[2].strip()}")
            for line in quality_lines[3:6]:
                if line.strip().startswith("- "):
                    lines.append(line.strip())
        lines.append("")

    for role in ROLE_NAMES:
        state = role_loop_state(role)
        status = "running" if state["running"] else "stopped"
        lines.append(f"**{role.title()}**: `{status}`")
        lines.append(state["summary"])
        lines.append("")
    return "\n".join(lines).strip()
