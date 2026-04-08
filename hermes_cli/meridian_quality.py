"""Meridian review-signal quality gate orchestration.

This module consumes Meridian workflow events and runs non-blocking quality and
security checks when tasks enter ``review``. Results are persisted in
``$HERMES_HOME/meridian/quality_gate_state.json`` and rendered as markdown
reports under ``$HERMES_HOME/meridian/review_signals/`` so Matthew and Fatih
can consume the same evidence.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_utils import atomic_json_write

from hermes_cli.meridian_runtime import EVENT_LOG_PATH, emit_meridian_event, parse_iso_datetime


STATE_PATH = get_hermes_home() / "meridian" / "quality_gate_state.json"
REPORTS_DIR = get_hermes_home() / "meridian" / "review_signals"
DEFAULT_REMOTE_WORKSPACE = "/home/umut/meridian"
DEFAULT_EVENT_TYPES = frozenset({"task_transitioned"})
DEFAULT_REVIEW_QUEUE = "review"

LANE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "name": "backend-quality",
        "kind": "quality",
        "path": "scripts/backend-quality-check.sh",
        "cwd": ".",
        "command": "bash scripts/backend-quality-check.sh",
    },
    {
        "name": "backend-security",
        "kind": "security",
        "path": "scripts/backend-security-check.sh",
        "cwd": ".",
        "command": "bash scripts/backend-security-check.sh",
    },
    {
        "name": "frontend-lint",
        "kind": "quality",
        "path": "frontend/package.json",
        "cwd": "frontend",
        "command": "npm run lint",
    },
    {
        "name": "frontend-build",
        "kind": "quality",
        "path": "frontend/package.json",
        "cwd": "frontend",
        "command": "npm run build",
    },
    {
        "name": "frontend-security",
        "kind": "security",
        "path": "frontend/package.json",
        "cwd": "frontend",
        "command": "npm run security:check",
    },
)


@dataclass(frozen=True)
class Executor:
    mode: str
    workspace: str
    host: str | None = None
    user: str | None = None
    key: str | None = None
    password: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    state = _read_json(path)
    if not isinstance(state.get("results"), dict):
        state["results"] = {}
    if not isinstance(state.get("processed_event_ids"), list):
        state["processed_event_ids"] = []
    if not isinstance(state.get("line_cursor"), int):
        state["line_cursor"] = 0
    return state


def _save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, state)


def _normalize_workspace(workspace: str | Path | None) -> str:
    if workspace is None:
        env_workspace = (os.getenv("HERMES_MERIDIAN_WORKSPACE") or "").strip()
        return env_workspace or DEFAULT_REMOTE_WORKSPACE
    return str(Path(workspace).expanduser()) if str(workspace).strip() else DEFAULT_REMOTE_WORKSPACE


def _terminal_ssh_settings() -> dict[str, str] | None:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return None
    terminal = config.get("terminal") or {}
    if str(terminal.get("backend") or "").strip().lower() != "ssh":
        return None
    host = str(os.getenv("TERMINAL_SSH_HOST") or terminal.get("ssh_host") or "").strip()
    user = str(os.getenv("TERMINAL_SSH_USER") or terminal.get("ssh_user") or "").strip()
    key = str(os.getenv("TERMINAL_SSH_KEY") or terminal.get("ssh_key") or "").strip()
    password = str(os.getenv("TERMINAL_SSH_PASSWORD") or "").strip()
    cwd = str(os.getenv("HERMES_MERIDIAN_WORKSPACE") or terminal.get("cwd") or "").strip()
    if not host or not user:
        return None
    return {
        "host": host,
        "user": user,
        "key": key,
        "password": password,
        "workspace": cwd or DEFAULT_REMOTE_WORKSPACE,
    }


def _quality_ssh_settings() -> dict[str, str] | None:
    host = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_HOST")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_HOST")
        or ""
    ).strip()
    user = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_USER")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_USER")
        or ""
    ).strip()
    key = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_KEY")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_KEY")
        or ""
    ).strip()
    password = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_PASSWORD")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_PASSWORD")
        or ""
    ).strip()
    workspace = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_WORKSPACE")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_WORKSPACE")
        or os.getenv("HERMES_MERIDIAN_WORKSPACE")
        or ""
    ).strip()
    if host and user:
        return {
            "host": host,
            "user": user,
            "key": key,
            "password": password,
            "workspace": workspace or DEFAULT_REMOTE_WORKSPACE,
        }
    return _terminal_ssh_settings()


def _build_executor(workspace: str | Path | None = None) -> Executor:
    normalized_workspace = _normalize_workspace(workspace)
    candidate = Path(normalized_workspace).expanduser()
    if candidate.exists():
        return Executor(mode="local", workspace=str(candidate.resolve()))
    ssh = _quality_ssh_settings()
    if ssh:
        return Executor(
            mode="ssh",
            workspace=ssh.get("workspace") or normalized_workspace,
            host=ssh.get("host"),
            user=ssh.get("user"),
            key=ssh.get("key"),
            password=ssh.get("password"),
        )
    return Executor(mode="local", workspace=str(candidate))


def _run_local(command: str, *, cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_ssh(executor: Executor, command: str, *, cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    remote_command = f"cd {shlex.quote(cwd)} && {command}"
    if executor.password:
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
            f"{executor.user}@{executor.host}",
            remote_command,
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
        if executor.key:
            cmd.extend(["-i", executor.key])
        cmd.extend([f"{executor.user}@{executor.host}", remote_command])
    env = os.environ.copy()
    if executor.password:
        env["SSHPASS"] = executor.password
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _run_command(executor: Executor, command: str, *, cwd: str, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    if executor.mode == "ssh":
        return _run_ssh(executor, command, cwd=cwd, timeout=timeout)
    return _run_local(command, cwd=cwd, timeout=timeout)


def _available_lanes(executor: Executor) -> list[dict[str, str]]:
    available: list[dict[str, str]] = []
    for lane in LANE_DEFINITIONS:
        if executor.mode == "local":
            probe = Path(executor.workspace) / lane["path"]
            if probe.exists():
                available.append(dict(lane))
        else:
            probe_cmd = f"test -e {shlex.quote(str(Path(executor.workspace) / lane['path']))}"
            result = _run_command(executor, probe_cmd, cwd=executor.workspace, timeout=30)
            if result.returncode == 0:
                available.append(dict(lane))
    return available


def _trim_output(text: str, *, max_lines: int = 80, max_chars: int = 5000) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    lines = normalized.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(normalized.splitlines()) - max_lines} more lines)"]
    clipped = "\n".join(lines)
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 18].rstrip() + "\n... (truncated)"
    return clipped


def _summarize_lanes(lanes: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"passed": 0, "failed": 0, "skipped": 0, "quality_failures": 0, "security_failures": 0}
    for lane in lanes:
        status = lane.get("status")
        if status == "passed":
            summary["passed"] += 1
        elif status == "failed":
            summary["failed"] += 1
            if lane.get("kind") == "security":
                summary["security_failures"] += 1
            else:
                summary["quality_failures"] += 1
        else:
            summary["skipped"] += 1
    return summary


def _render_report(result: dict[str, Any]) -> str:
    lines = [
        f"# Meridian Review Signals: {result['task_id']}",
        "",
        f"- scanned_at: {result.get('scanned_at', '')}",
        f"- workspace: `{result.get('workspace', '')}`",
        f"- executor: `{result.get('executor', '')}`",
        f"- status: `{result.get('status', '')}`",
        f"- summary: {result.get('summary', '')}",
    ]
    if result.get("trigger_event_id"):
        lines.append(f"- trigger_event_id: `{result['trigger_event_id']}`")
    if result.get("triggered_by"):
        lines.append(f"- triggered_by: `{result['triggered_by']}`")
    lines.extend(["", "## Lanes", ""])
    for lane in result.get("lanes", []):
        lines.append(
            f"### {lane.get('name')} [{lane.get('status')}]"
        )
        lines.append("")
        lines.append(f"- kind: `{lane.get('kind')}`")
        lines.append(f"- command: `{lane.get('command')}`")
        lines.append(f"- exit_code: `{lane.get('exit_code')}`")
        lines.append(f"- duration_seconds: `{lane.get('duration_seconds')}`")
        output = lane.get("output") or ""
        if output:
            lines.extend(["", "```text", output, "```"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_report(result: dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{result['task_id']}.md"
    report_path.write_text(_render_report(result), encoding="utf-8")
    return str(report_path)


def _task_result_summary(result: dict[str, Any]) -> str:
    lanes = result.get("lanes") or []
    summary = _summarize_lanes(lanes)
    parts: list[str] = []
    if summary["failed"]:
        parts.append(f"{summary['failed']} lane failed")
    if summary["passed"]:
        parts.append(f"{summary['passed']} passed")
    if summary["skipped"]:
        parts.append(f"{summary['skipped']} skipped")
    if summary["security_failures"]:
        parts.append(f"{summary['security_failures']} security")
    if summary["quality_failures"]:
        parts.append(f"{summary['quality_failures']} quality")
    return ", ".join(parts) if parts else "no lanes executed"


def latest_quality_result(task_id: str, *, state_path: Path = STATE_PATH) -> dict[str, Any] | None:
    state = _load_state(state_path)
    result = state.get("results", {}).get(task_id)
    return dict(result) if isinstance(result, dict) else None


def quality_brief_for_task(task_id: str, *, state_path: Path = STATE_PATH) -> str:
    result = latest_quality_result(task_id, state_path=state_path)
    if not result:
        return ""
    report_path = result.get("report_path") or "-"
    return (
        f"Quality gate: `{result.get('status', 'unknown')}`"
        f" | {result.get('summary', _task_result_summary(result))}"
        f" | report: `{report_path}`"
    )


def _scan_task(
    task_id: str,
    *,
    executor: Executor,
    triggered_by: str,
    trigger_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    lanes: list[dict[str, Any]] = []
    for lane in _available_lanes(executor):
        lane_started = time.monotonic()
        cwd = str(Path(executor.workspace) / lane["cwd"]) if executor.mode == "local" else str(Path(executor.workspace) / lane["cwd"])
        completed = _run_command(executor, lane["command"], cwd=cwd)
        combined_output = _trim_output("\n".join(part for part in (completed.stdout, completed.stderr) if part))
        lanes.append(
            {
                "name": lane["name"],
                "kind": lane["kind"],
                "command": lane["command"],
                "cwd": cwd,
                "status": "passed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "duration_seconds": round(time.monotonic() - lane_started, 2),
                "output": combined_output,
            }
        )

    if not lanes:
        lanes.append(
            {
                "name": "no-op",
                "kind": "quality",
                "command": "",
                "cwd": executor.workspace,
                "status": "skipped",
                "exit_code": 0,
                "duration_seconds": 0.0,
                "output": "No supported backend/frontend quality lanes were detected in this workspace.",
            }
        )

    summary = _summarize_lanes(lanes)
    status = "passed" if summary["failed"] == 0 else "failed"
    result = {
        "task_id": task_id,
        "workspace": executor.workspace,
        "executor": executor.mode if executor.mode == "local" else f"ssh:{executor.user}@{executor.host}",
        "trigger_event_id": trigger_event.get("id") if isinstance(trigger_event, dict) else None,
        "triggered_by": triggered_by,
        "scanned_at": _isoformat(_utcnow()),
        "duration_seconds": round(time.monotonic() - started, 2),
        "status": status,
        "summary": _task_result_summary({"lanes": lanes}),
        "lanes": lanes,
    }
    result["report_path"] = _write_report(result)
    return result


def _emit_result_event(result: dict[str, Any]) -> None:
    event_type = "meridian_quality_gate_completed" if result.get("status") == "passed" else "meridian_quality_gate_failed"
    emit_meridian_event(
        event_type,
        {
            "task_id": result.get("task_id"),
            "workspace": result.get("workspace"),
            "report_path": result.get("report_path"),
            "summary": result.get("summary"),
            "status": result.get("status"),
            "trigger_event_id": result.get("trigger_event_id"),
        },
    )


def _new_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        lines = EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    cursor = int(state.get("line_cursor") or 0)
    if cursor < 0 or cursor > len(lines):
        cursor = 0
    new_lines = lines[cursor:]
    state["line_cursor"] = len(lines)
    events: list[dict[str, Any]] = []
    for raw in new_lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _review_events(events: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    processed = set(str(item) for item in state.get("processed_event_ids") or [])
    selected: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("id") or "")
        if event_id and event_id in processed:
            continue
        if event.get("type") not in DEFAULT_EVENT_TYPES:
            continue
        if event.get("to_queue") != DEFAULT_REVIEW_QUEUE:
            continue
        if not event.get("task_id"):
            continue
        selected.append(event)
    return selected


def run_quality_gate_once(
    workspace: str | Path | None = None,
    *,
    task_id: str | None = None,
    force: bool = False,
    state_path: Path = STATE_PATH,
) -> dict[str, Any]:
    state = _load_state(state_path)
    executor = _build_executor(workspace)
    results: list[dict[str, Any]] = []

    if task_id:
        result = _scan_task(task_id, executor=executor, triggered_by="manual")
        state["results"][task_id] = result
        results.append(result)
        _emit_result_event(result)
        _save_state(state, state_path)
        return {"scanned": results, "count": len(results), "source": "manual"}

    events = _new_events(state)
    review_events = _review_events(events, state)
    if force and not review_events:
        last_review_task = None
        for event in reversed(events):
            if event.get("to_queue") == DEFAULT_REVIEW_QUEUE and event.get("task_id"):
                last_review_task = event.get("task_id")
                break
        if last_review_task:
            review_events = [{"id": "", "task_id": last_review_task, "type": "manual_force"}]

    for event in review_events:
        result = _scan_task(
            str(event["task_id"]),
            executor=executor,
            triggered_by=str(event.get("type") or "event"),
            trigger_event=event,
        )
        state["results"][str(event["task_id"])] = result
        results.append(result)
        event_id = str(event.get("id") or "")
        if event_id:
            processed = list(state.get("processed_event_ids") or [])
            processed.append(event_id)
            state["processed_event_ids"] = processed[-200:]
        _emit_result_event(result)

    _save_state(state, state_path)
    return {"scanned": results, "count": len(results), "source": "event"}


def format_quality_status(task_id: str | None = None, *, state_path: Path = STATE_PATH) -> str:
    state = _load_state(state_path)
    results = state.get("results") or {}
    if task_id:
        result = results.get(task_id)
        if not isinstance(result, dict):
            return f"No quality-gate result recorded for `{task_id}`."
        lines = [
            f"Meridian quality gate `{task_id}`",
            f"  Status:   {result.get('status')}",
            f"  Summary:  {result.get('summary')}",
            f"  Scanned:  {result.get('scanned_at')}",
            f"  Report:   {result.get('report_path')}",
        ]
        for lane in result.get("lanes", []):
            lines.append(
                f"  - {lane.get('name')}: {lane.get('status')} (exit={lane.get('exit_code')}, {lane.get('duration_seconds')}s)"
            )
        return "\n".join(lines)

    ordered = sorted(
        (
            item
            for item in results.values()
            if isinstance(item, dict)
        ),
        key=lambda item: parse_iso_datetime(item.get("scanned_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if not ordered:
        return "No quality-gate results recorded yet."
    lines = ["Meridian quality gate", f"  Recorded tasks: {len(ordered)}"]
    for item in ordered[:8]:
        lines.append(
            f"  - {item.get('task_id')}: {item.get('status')} | {item.get('summary')} | {item.get('scanned_at')}"
        )
    return "\n".join(lines)
