"""Meridian customer-support tickets and role status helpers."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_cli.meridian_dispatcher import _resolve_workspace_path


ROLE_NAMES = ("philip", "fatih", "matthew")
SUPPORT_QUEUES = ("inbox", "responded", "summaries")
HEADER_RE = re.compile(r"^=== (?P<timestamp>\S+) \[(?P<role>[a-z]+)\] profile=(?P<profile>\S+) workspace=(?P<workspace>.+) ===$")


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


def build_roles_status_text() -> str:
    lines = ["🧭 **Meridian Role Status**", ""]
    for role in ROLE_NAMES:
        state = role_loop_state(role)
        status = "running" if state["running"] else "stopped"
        lines.append(f"**{role.title()}**: `{status}`")
        lines.append(state["summary"])
        lines.append("")
    return "\n".join(lines).strip()
