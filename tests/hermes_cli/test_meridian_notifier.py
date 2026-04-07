from __future__ import annotations

from hermes_cli import meridian_notifier as mn


def test_build_changes_labels_core_queue_transitions():
    previous = {
        "task-a.md": mn.TaskSnapshot(queue="ready", filename="task-a.md"),
        "task-b.md": mn.TaskSnapshot(queue="in_progress", filename="task-b.md"),
        "task-c.md": mn.TaskSnapshot(queue="review", filename="task-c.md"),
    }
    current = {
        "task-a.md": mn.TaskSnapshot(queue="in_progress", filename="task-a.md"),
        "task-b.md": mn.TaskSnapshot(queue="review", filename="task-b.md"),
        "task-c.md": mn.TaskSnapshot(queue="done", filename="task-c.md"),
        "task-d.md": mn.TaskSnapshot(queue="waiting_human", filename="task-d.md"),
    }

    changes = mn.build_changes(previous, current)
    kinds = {item["task"]: item["kind"] for item in changes}

    assert kinds["task-a.md"] == "started"
    assert kinds["task-b.md"] == "review"
    assert kinds["task-c.md"] == "done"
    assert kinds["task-d.md"] == "needs_input"


def test_run_waiting_human_notifier_detects_changes_and_formats_brief(tmp_path, monkeypatch):
    state_path = tmp_path / "waiting.json"
    first = {
        "task-a.md": mn.TaskSnapshot(queue="waiting_human", filename="task-a.md"),
        "task-b.md": mn.TaskSnapshot(queue="done", filename="task-b.md"),
    }
    second = {
        "task-a.md": mn.TaskSnapshot(queue="waiting_human", filename="task-a.md"),
        "task-c.md": mn.TaskSnapshot(queue="waiting_human", filename="task-c.md"),
    }
    snapshots = iter([first, second])
    monkeypatch.setattr(mn, "collect_snapshot", lambda: next(snapshots))

    result1 = mn.run_waiting_human_notifier(state_path=state_path)
    result2 = mn.run_waiting_human_notifier(state_path=state_path)

    assert result1["has_waiting_human"] is True
    assert result1["changed"] is True
    assert "task-a.md" in result1["brief"]

    assert result2["has_waiting_human"] is True
    assert result2["changed"] is True
    assert "task-c.md" in result2["brief"]


def test_tickets_needing_human_only_when_agent_updated_after_human_reply():
    silent = mn.SupportTicketSnapshot(
        ticket_id="20260408001",
        queue="inbox",
        summary="silent",
        status="human_replied",
        updated_at="2026-04-08T10:00:00+00:00",
        last_human_reply_at="2026-04-08T10:00:00+00:00",
    )
    active = mn.SupportTicketSnapshot(
        ticket_id="20260408002",
        queue="inbox",
        summary="needs answer",
        status="pending_role",
        updated_at="2026-04-08T11:00:00+00:00",
        last_human_reply_at="2026-04-08T10:00:00+00:00",
    )

    result = mn.tickets_needing_human(
        {
            silent.ticket_id: silent,
            active.ticket_id: active,
        }
    )

    assert [item.ticket_id for item in result] == ["20260408002"]


def test_run_waiting_human_notifier_includes_support_ticket_followups(tmp_path, monkeypatch):
    state_path = tmp_path / "waiting.json"
    task_snapshots = iter([{}, {}])
    support_snapshots = iter(
        [
            {
                "20260408001": mn.SupportTicketSnapshot(
                    ticket_id="20260408001",
                    queue="inbox",
                    summary="already answered",
                    status="human_replied",
                    updated_at="2026-04-08T10:00:00+00:00",
                    last_human_reply_at="2026-04-08T10:00:00+00:00",
                ),
            },
            {
                "20260408001": mn.SupportTicketSnapshot(
                    ticket_id="20260408001",
                    queue="inbox",
                    summary="already answered",
                    status="human_replied",
                    updated_at="2026-04-08T10:00:00+00:00",
                    last_human_reply_at="2026-04-08T10:00:00+00:00",
                ),
                "20260408002": mn.SupportTicketSnapshot(
                    ticket_id="20260408002",
                    queue="inbox",
                    summary="agent asked follow-up",
                    status="pending_role",
                    updated_at="2026-04-08T11:00:00+00:00",
                    last_human_reply_at="2026-04-08T10:30:00+00:00",
                ),
            },
        ]
    )
    monkeypatch.setattr(mn, "collect_snapshot", lambda: next(task_snapshots))
    monkeypatch.setattr(mn, "collect_support_snapshot", lambda: next(support_snapshots))

    result1 = mn.run_waiting_human_notifier(state_path=state_path)
    result2 = mn.run_waiting_human_notifier(state_path=state_path)

    assert result1["has_support_waiting"] is False
    assert result2["has_support_waiting"] is True
    assert result2["changed"] is True
    assert "20260408002" in result2["brief"]
