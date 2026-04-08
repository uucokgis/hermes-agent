from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hermes_cli import meridian_support as ms


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "meridian"
    for queue in ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt"):
        (workspace / "tasks" / queue).mkdir(parents=True, exist_ok=True)
    (workspace / ".git").mkdir()
    return workspace


def _touch_task(path: Path, name: str) -> None:
    target = path / name
    target.write_text("---\nid: task\n---\n", encoding="utf-8")


def test_create_and_reply_ticket(tmp_path, monkeypatch):
    workspace = tmp_path / "meridian"
    (workspace / "tasks").mkdir(parents=True)
    monkeypatch.setattr(ms, "_resolve_workspace_path", lambda _workspace=None: workspace)

    created = ms.create_support_ticket(
        summary="Investigate drawing editor state",
        message="Please check the drawing editor issue.",
        target_role="fatih",
        sender="umut",
        now=datetime(2026, 4, 7, 10, 0, tzinfo=timezone.utc),
    )

    assert created.ticket_id == "20260407001"
    assert created.path.exists()
    assert created.metadata["target_role"] == "fatih"

    looked_up = ms.get_support_ticket(created.ticket_id, workspace)
    assert looked_up is not None
    assert looked_up.ticket_id == created.ticket_id

    updated = ms.append_human_reply(
        created.ticket_id,
        message="Actually prioritize selection sync first.",
        sender="umut",
        workspace=workspace,
        now=datetime(2026, 4, 7, 10, 5, tzinfo=timezone.utc),
    )

    assert updated.metadata["status"] == "human_replied"
    assert "selection sync first" in updated.body


def test_list_support_tickets_orders_most_recent_first(tmp_path, monkeypatch):
    workspace = tmp_path / "meridian"
    (workspace / "tasks").mkdir(parents=True)
    monkeypatch.setattr(ms, "_resolve_workspace_path", lambda _workspace=None: workspace)

    ms.create_support_ticket(
        summary="Older",
        message="older ticket",
        target_role="philip",
        sender="umut",
        now=datetime(2026, 4, 7, 8, 0, tzinfo=timezone.utc),
    )
    newer = ms.create_support_ticket(
        summary="Newer",
        message="newer ticket",
        target_role="matthew",
        sender="umut",
        now=datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc),
    )

    tickets = ms.list_support_tickets(workspace, limit=5)
    assert tickets[0].ticket_id == newer.ticket_id


def test_role_loop_state_reads_latest_summary(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    loop_dir = fake_home / ".hermes" / "meridian" / "loops"
    loop_dir.mkdir(parents=True)
    (loop_dir / "philip.loop.log").write_text(
        "=== 2026-04-07T10:00:00+00:00 [philip] profile=meridian-philip workspace=/tmp/meridian ===\n"
        "Queue looks healthy and no urgent PM work is required.\n"
        "UI follow-up deferred to tomorrow.\n",
        encoding="utf-8",
    )
    (loop_dir / "philip.loop.pid").write_text("999999", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr(ms.os, "kill", lambda _pid, _sig: None)

    state = ms.role_loop_state("philip")
    assert state["running"] is True
    assert "Queue looks healthy" in state["summary"]


def test_build_roles_status_text_includes_workspace_queue_focus_and_commits(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path)
    _touch_task(workspace / "tasks" / "done", "PHILIP-20260405-010-design-and-scope-drawing-editor-workflow.md")
    _touch_task(workspace / "tasks" / "review", "PHILIP-20260405-011-define-attribute-table-provider-contracts-and-registry.md")
    _touch_task(workspace / "tasks" / "in_progress", "W3-001-layer-visibility-tests.md")
    _touch_task(workspace / "tasks" / "backlog", "PHILIP-20260405-010-build-docked-attribute-table-shell-and-store.md")
    _touch_task(workspace / "tasks" / "ready", "ROUTE-CSV-Upload-Panel.md")

    monkeypatch.setattr(ms, "resolve_support_workspace", lambda _workspace=None: workspace)
    monkeypatch.setattr(
        ms,
        "role_loop_state",
        lambda role: {
            "role": role,
            "running": role != "matthew",
            "summary": f"{role} summary",
            "pid": None,
            "header": "",
            "log_path": "",
        },
    )
    monkeypatch.setattr(ms, "_git_headlines", lambda _workspace, limit=4: ["abc123 feature: ship route sidebar"])

    text = ms.build_roles_status_text()

    assert "backlog=1" in text
    assert "review=1" in text
    assert "Recently Done" in text
    assert "Needs Review" in text
    assert "Tracked Topics" in text
    assert "Drawing widget" in text
    assert "Attribute table" in text
    assert "Routing / CSV" in text
    assert "Layer visibility" in text
    assert "Recent Commits" in text
    assert "Philip" in text
    assert "Matthew" in text


def test_build_roles_status_text_uses_ssh_probe_when_local_workspace_missing(monkeypatch):
    missing = Path("/tmp/definitely-missing-meridian-workspace")
    monkeypatch.setattr(ms, "resolve_support_workspace", lambda _workspace=None: missing)
    monkeypatch.setattr(
        ms,
        "_ssh_terminal_settings",
        lambda: {
            "host": "192.168.1.107",
            "user": "umut",
            "key": "",
            "cwd": "/home/umut/meridian",
        },
    )
    monkeypatch.setattr(
        ms,
        "_collect_remote_workspace_summary",
        lambda settings: {
            "source": "ssh",
            "workspace": settings["cwd"],
            "remote_host": settings["host"],
            "remote_user": settings["user"],
            "queue_counts": {
                "backlog": 2,
                "ready": 1,
                "in_progress": 1,
                "review": 2,
                "done": 5,
                "debt": 1,
            },
            "recent_done": ["PHILIP-011-Fatih-COMPLETED.md"],
            "recent_review": ["MATTHEW-20260407-REVIEW-PASS-4-SUMMARY.md"],
            "recent_in_progress": ["PHILIP-011-Fatih-IMPLEMENTATION-20260407-FINAL.md"],
            "focus_items": [
                {"label": "Attribute table", "items": "review: PHILIP-20260405-011-define-attribute-table-provider-contracts-and-registry.md"},
            ],
            "recent_commits": ["a797246 fix: permission bug"],
        },
    )
    monkeypatch.setattr(
        ms,
        "role_loop_state",
        lambda role: {
            "role": role,
            "running": True,
            "summary": f"{role} loop",
            "pid": None,
            "header": "",
            "log_path": "",
        },
    )

    text = ms.build_roles_status_text()

    assert "umut@192.168.1.107:/home/umut/meridian" in text
    assert "PHILIP-011-Fatih-COMPLETED.md" in text
    assert "MATTHEW-20260407-REVIEW-PASS-4-SUMMARY.md" in text
    assert "Attribute table" in text


def test_build_roles_status_text_reads_legacy_in_progress_alias(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path)
    legacy_dir = workspace / "tasks" / "in-progress"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    _touch_task(legacy_dir, "PHILIP-legacy-task.md")

    monkeypatch.setattr(ms, "resolve_support_workspace", lambda _workspace=None: workspace)
    monkeypatch.setattr(
        ms,
        "role_loop_state",
        lambda role: {
            "role": role,
            "running": True,
            "summary": f"{role} summary",
            "pid": None,
            "header": "",
            "log_path": "",
        },
    )
    monkeypatch.setattr(ms, "_git_headlines", lambda _workspace, limit=4: [])

    text = ms.build_roles_status_text()

    assert "in_progress=1" in text
    assert "PHILIP-legacy-task.md" in text
