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
