from argparse import Namespace
from pathlib import Path


def _write_task(path: Path, task_id: str, *, branch: str | None = None) -> None:
    lines = [f"id: {task_id}", f"title: {task_id}"]
    if branch:
        lines.append(f"pr_branch: {branch}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for queue in ("backlog", "ready", "in_progress", "review", "done", "debt"):
        (workspace / "tasks" / queue).mkdir(parents=True, exist_ok=True)
    return workspace


def test_collect_snapshot_prefers_review_over_ready_and_exposes_branch(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")
    _write_task(
        workspace / "tasks" / "review" / "review-task.md",
        "TASK-2",
        branch="task/review-loop",
    )

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["active_persona"] == "matthew"
    assert snapshot["workflow_state"] == "review"
    assert snapshot["waiting_on"] == "matthew"
    assert snapshot["active_task_id"] == "TASK-2"
    assert snapshot["current_branch"] == "task/review-loop"
    assert snapshot["queue_counts"]["ready"] == 1
    assert snapshot["queue_counts"]["review"] == 1


def test_collect_snapshot_keeps_review_loop_locked_to_in_progress_task(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "in_progress" / "loop-task.md", "TASK-LOOP")
    _write_task(workspace / "tasks" / "ready" / "new-task.md", "TASK-NEW")

    snapshot = md.collect_meridian_snapshot(
        workspace,
        state={"review_loop_task_id": "TASK-LOOP"},
    )

    assert snapshot["active_persona"] == "fatih"
    assert snapshot["workflow_state"] == "in_progress"
    assert snapshot["active_task_id"] == "TASK-LOOP"
    assert snapshot["waiting_on"] == "fatih"


def test_collect_snapshot_waiting_human_overrides_auto_dispatch(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "review-task.md", "TASK-9")

    snapshot = md.collect_meridian_snapshot(
        workspace,
        state={"waiting_human": True, "review_loop_task_id": "TASK-9"},
    )

    assert snapshot["active_persona"] == "idle"
    assert snapshot["workflow_state"] == "waiting_human"
    assert snapshot["waiting_on"] == "human_confirmation"
    assert snapshot["active_task_id"] == "TASK-9"


def test_dispatch_only_suggests_new_wakeups_once_per_transition(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    first = md.dispatch_meridian(workspace)
    second = md.dispatch_meridian(workspace)

    assert first["should_dispatch"] is True
    assert second["should_dispatch"] is False
    assert second["last_dispatched_persona"] == "fatih"
    assert second["last_dispatched_task_id"] == "TASK-1"


def test_meridian_command_status_prints_expected_fields(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "backlog" / "backlog-task.md", "TASK-B")

    rc = md.meridian_command(Namespace(meridian_command="status", workspace=str(workspace)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian status" in out
    assert "Active persona: philip" in out
    assert "Workflow state: backlog" in out
    assert "Waiting on:     philip" in out
    assert "backlog=1" in out


def test_main_routes_meridian_status_subcommand(monkeypatch):
    import sys
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_meridian(args):
        captured["command"] = args.command
        captured["subcommand"] = args.meridian_command
        captured["workspace"] = args.workspace

    monkeypatch.setattr(main_mod, "cmd_meridian", fake_cmd_meridian)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "meridian", "status", "--workspace", "/tmp/meridian-workspace"],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "status",
        "workspace": "/tmp/meridian-workspace",
    }
