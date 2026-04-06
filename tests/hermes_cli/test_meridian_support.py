from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hermes_cli import meridian_support as ms


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
