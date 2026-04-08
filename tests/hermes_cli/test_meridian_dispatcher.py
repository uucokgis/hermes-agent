from argparse import Namespace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import yaml
import pytest

def _write_task(
    path: Path,
    task_id: str,
    *,
    branch: str | None = None,
    metadata: dict | None = None,
) -> None:
    payload = {
        "id": task_id,
        "title": task_id,
        **(metadata or {}),
    }
    if branch:
        payload["pr_branch"] = branch
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(payload, sort_keys=False).strip() + "\n---\n",
        encoding="utf-8",
    )


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for queue in ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt"):
        (workspace / "tasks" / queue).mkdir(parents=True, exist_ok=True)
    return workspace


def _write_review_decision(path: Path, **overrides) -> None:
    payload = {
        "review_schema_version": 1,
        "review_id": "REVIEW-20260408-001",
        "review_task_id": "TASK-REVIEW",
        "review_kind": "decision",
        "review_outcome": "request_changes",
        "decision_bucket": "blocking",
        "reviewer": "matthew",
        "status": "final",
        "required_actions": [{"id": "RA-1", "summary": "Fix failing case", "status": "open"}],
        "updated_at": "2026-04-08T02:00:00+00:00",
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(payload, sort_keys=False).strip() + "\n---\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolated_meridian_events(tmp_path, monkeypatch):
    from hermes_cli import meridian_runtime as mr

    monkeypatch.setattr(mr, "EVENT_LOG_PATH", tmp_path / ".hermes" / "meridian" / "events.jsonl")


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
    assert snapshot["planned_actions"][0]["task_id"] == "TASK-2"
    assert snapshot["planned_actions"][0]["actor"] == "matthew"


def test_collect_snapshot_auto_discovers_meridian_workspace_from_home(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    home = tmp_path / "home"
    workspace = home / "meridian"
    for queue in ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt"):
        (workspace / "tasks" / queue).mkdir(parents=True, exist_ok=True)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(md.Path, "home", lambda: home)
    other_cwd = tmp_path / "cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    _write_task(workspace / "tasks" / "backlog" / "backlog-task.md", "TASK-B")

    snapshot = md.collect_meridian_snapshot(None)

    assert snapshot["workspace"] == str(workspace.resolve())
    assert snapshot["active_persona"] == "philip"
    assert snapshot["active_task_id"] == "TASK-B"


def test_collect_snapshot_explicit_missing_workspace_raises_clear_error_with_remote_hint(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(
        md,
        "load_config",
        lambda: {
            "terminal": {
                "backend": "ssh",
                "ssh_host": "192.168.1.107",
                "cwd": "/home/umut/meridian",
            }
        },
    )

    with pytest.raises(FileNotFoundError) as exc:
        md.collect_meridian_snapshot("/home/umut/meridian")

    message = str(exc.value)
    assert "does not exist on this machine" in message
    assert "192.168.1.107:/home/umut/meridian" in message


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


def test_collect_snapshot_stale_waiting_human_state_does_not_override_canonical_queues(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "review-task.md", "TASK-9")

    snapshot = md.collect_meridian_snapshot(
        workspace,
        state={"waiting_human": True, "review_loop_task_id": "TASK-9"},
    )

    assert snapshot["active_persona"] == "matthew"
    assert snapshot["workflow_state"] == "review"
    assert snapshot["waiting_on"] == "matthew"
    assert snapshot["active_task_id"] == "TASK-9"


def test_collect_snapshot_reads_waiting_human_from_canonical_queue(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "waiting_human" / "hold-task.md", "TASK-H")

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["active_persona"] == "idle"
    assert snapshot["workflow_state"] == "waiting_human"
    assert snapshot["waiting_on"] == "human_confirmation"
    assert snapshot["active_task_id"] == "TASK-H"
    assert snapshot["planned_actions"][0]["kind"] == "hold_waiting_human"


def test_dispatch_auto_applies_review_approval_when_no_open_actions(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "task-1.md", "TASK-1")
    _write_review_decision(
        workspace / "tasks" / "review" / "decisions" / "TASK-1-decision.md",
        review_task_id="TASK-1",
        review_outcome="approved",
        decision_bucket="passed",
        required_actions=[],
        transition_recommendation={"from_queue": "review", "to_queue": "done"},
    )

    snapshot = md.dispatch_meridian(workspace)

    assert snapshot["workflow_state"] == "idle"
    assert (workspace / "tasks" / "done" / "task-1.md").exists()


def test_dispatch_does_not_auto_apply_done_transition_with_open_actions(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "task-1.md", "TASK-1")
    _write_review_decision(
        workspace / "tasks" / "review" / "decisions" / "TASK-1-decision.md",
        review_task_id="TASK-1",
        review_outcome="approved",
        decision_bucket="passed",
        required_actions=[{"id": "RA-1", "summary": "Follow-up needed", "status": "open"}],
        transition_recommendation={"from_queue": "review", "to_queue": "done"},
    )

    snapshot = md.dispatch_meridian(workspace)

    assert snapshot["workflow_state"] == "review"
    assert snapshot["auto_review_transition"]["status"] == "blocked_open_actions"
    assert (workspace / "tasks" / "review" / "task-1.md").exists()


def test_collect_snapshot_does_not_wedge_waiting_human_from_stale_derived_state(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    snapshot = md.collect_meridian_snapshot(
        workspace,
        state={"waiting_human": True, "waiting_on": "human_confirmation", "review_loop_task_id": "TASK-OLD"},
    )

    assert snapshot["waiting_human"] is False
    assert snapshot["workflow_state"] == "ready"
    assert snapshot["waiting_on"] == "fatih"
    assert snapshot["active_task_id"] == "TASK-1"


def test_dispatch_only_suggests_new_wakeups_once_per_transition(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(md, "_local_policy_window", lambda now: "normal")

    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    first = md.dispatch_meridian(workspace)
    second = md.dispatch_meridian(workspace)

    assert first["should_dispatch"] is True
    assert second["should_dispatch"] is False
    assert second["last_dispatched_persona"] == "fatih"
    assert second["last_dispatched_task_id"] == "TASK-1"
    assert second["last_dispatch_results"]["suppressed_actions"][0]["reason"] == "worker_lease_active"


def test_dispatch_creates_worker_lease_and_idempotency_key(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md
    from hermes_cli import meridian_runtime as mr

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(md, "_local_policy_window", lambda now: "normal")
    monkeypatch.setattr(mr, "EVENT_LOG_PATH", tmp_path / ".hermes" / "meridian" / "events.jsonl")

    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    result = md.dispatch_meridian(workspace)

    assert result["should_dispatch"] is True
    dispatched = result["last_dispatch_results"]["dispatched_actions"][0]
    lease = result["worker_leases"][0]
    assert dispatched["actor"] == "fatih"
    assert dispatched["task_id"] == "TASK-1"
    assert dispatched["idempotency_key"]
    assert lease["action_identity"] == "fatih:TASK-1:start_ready_task"
    assert lease["idempotency_key"] == dispatched["idempotency_key"]
    assert "meridian_dispatch_completed" in mr.EVENT_LOG_PATH.read_text(encoding="utf-8")


def test_dispatch_allows_retry_after_worker_lease_expiry(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    first_time = datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc)
    second_time = first_time + timedelta(minutes=31)

    first = md.dispatch_meridian(workspace, now=first_time)
    second = md.dispatch_meridian(workspace, now=second_time)

    assert first["should_dispatch"] is True
    assert second["should_dispatch"] is True
    assert second["last_dispatch_results"]["dispatched_actions"][0]["idempotency_key"] == first["last_dispatch_results"]["dispatched_actions"][0]["idempotency_key"]
    assert second["worker_leases"][0]["acquired_at"] == second_time.isoformat()


def test_dispatch_respects_active_orchestrator_lease(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "orchestrator_lease": {
                    "run_id": "existing-run",
                    "workspace": str(workspace),
                    "acquired_at": "2026-04-05T12:00:00+00:00",
                    "expires_at": "2026-04-05T12:02:00+00:00",
                    "status": "active",
                }
            }
        ),
        encoding="utf-8",
    )

    result = md.dispatch_meridian(
        workspace,
        now=datetime(2026, 4, 5, 12, 1, tzinfo=timezone.utc),
    )

    assert result["should_dispatch"] is False
    assert result["dispatch_blocked_reason"] == "orchestrator_lease_active"
    assert result["orchestrator_lease"]["run_id"] == "existing-run"


def test_reconcile_detects_drift_and_emits_event(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md
    from hermes_cli import meridian_runtime as mr

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    event_path = tmp_path / ".hermes" / "meridian" / "events.jsonl"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(mr, "EVENT_LOG_PATH", event_path)

    _write_task(workspace / "tasks" / "backlog" / "task-1.md", "TASK-1")
    initial = md.reconcile_meridian(workspace, now=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc))
    assert initial["drift_detected"] is False

    _write_task(workspace / "tasks" / "ready" / "task-2.md", "TASK-2")
    drifted = md.reconcile_meridian(workspace, now=datetime(2026, 4, 5, 12, 5, tzinfo=timezone.utc))

    assert drifted["drift_detected"] is True
    events = event_path.read_text(encoding="utf-8")
    assert "meridian_reconciled" in events
    assert "meridian_drift_detected" in events


def test_reconcile_emits_stale_task_event(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md
    from hermes_cli import meridian_runtime as mr

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    event_path = tmp_path / ".hermes" / "meridian" / "events.jsonl"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(mr, "EVENT_LOG_PATH", event_path)

    stale_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    _write_task(
        workspace / "tasks" / "review" / "stale-task.md",
        "TASK-STALE",
        metadata={"updated_at": stale_time},
    )

    md.reconcile_meridian(workspace, now=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc))

    events = event_path.read_text(encoding="utf-8")
    assert "stale_task_detected" in events


def test_reconcile_deduplicates_unchanged_stale_task_events(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md
    from hermes_cli import meridian_runtime as mr

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    event_path = tmp_path / ".hermes" / "meridian" / "events.jsonl"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(mr, "EVENT_LOG_PATH", event_path)

    stale_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    _write_task(
        workspace / "tasks" / "review" / "stale-task.md",
        "TASK-STALE",
        metadata={"updated_at": stale_time},
    )

    md.reconcile_meridian(workspace, now=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc))
    first_events = event_path.read_text(encoding="utf-8").splitlines()
    first_stale_count = sum("stale_task_detected" in line for line in first_events)

    md.reconcile_meridian(workspace, now=datetime(2026, 4, 5, 12, 5, tzinfo=timezone.utc))
    second_events = event_path.read_text(encoding="utf-8").splitlines()
    second_stale_count = sum("stale_task_detected" in line for line in second_events)

    assert first_stale_count == 1
    assert second_stale_count == 1


def test_meridian_command_reconcile_prints_recovery_status(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md
    from hermes_cli import meridian_runtime as mr

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(mr, "EVENT_LOG_PATH", tmp_path / ".hermes" / "meridian" / "events.jsonl")
    _write_task(workspace / "tasks" / "backlog" / "task-1.md", "TASK-1")

    rc = md.meridian_command(Namespace(meridian_command="reconcile", workspace=str(workspace)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Reconcile result:" in out


def test_planner_replenishes_ready_while_delivery_is_active(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "in_progress" / "loop.md", "TASK-LOOP")
    _write_task(
        workspace / "tasks" / "backlog" / "promotable.md",
        "TASK-PROMOTE",
        metadata={"acceptance_criteria": ["done means done"], "priority": "high"},
    )

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["active_persona"] == "fatih"
    assert snapshot["planned_actions"][0]["actor"] == "fatih"
    replenish = next(action for action in snapshot["planned_actions"] if action["kind"] == "replenish_ready")
    assert replenish["actor"] == "philip"
    assert replenish["task_ids"] == ["TASK-PROMOTE"]


def test_planner_keeps_philip_scanning_backlog_during_active_delivery(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "review.md", "TASK-REVIEW")
    _write_task(workspace / "tasks" / "backlog" / "future.md", "TASK-BG")

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["planned_actions"][0]["actor"] == "matthew"
    philip_action = next(action for action in snapshot["planned_actions"] if action["actor"] == "philip")
    assert philip_action["kind"] == "background_backlog_scan"
    assert philip_action["task_id"] == "TASK-BG"


def test_night_patrol_keeps_fatih_from_starting_new_ready_work(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(md, "_local_policy_window", lambda now: "night_patrol")

    _write_task(workspace / "tasks" / "ready" / "ready.md", "TASK-READY")
    _write_task(workspace / "tasks" / "backlog" / "future.md", "TASK-BG")

    snapshot = md.collect_meridian_snapshot(workspace)
    start_ready = next(action for action in snapshot["planned_actions"] if action["kind"] == "start_ready_task")
    matthew_patrol = next(action for action in snapshot["planned_actions"] if action["actor"] == "matthew")

    assert start_ready["dispatchable"] is False
    assert matthew_patrol["kind"] == "night_architecture_patrol"

    dispatched = md.dispatch_meridian(workspace)["last_dispatch_results"]["dispatched_actions"]
    actors = {action["actor"] for action in dispatched}
    assert "fatih" not in actors
    assert "matthew" in actors


def test_morning_window_keeps_only_philip_background_planning(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(md, "_local_policy_window", lambda now: "philip_morning")

    _write_task(workspace / "tasks" / "backlog" / "future.md", "TASK-BG")

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["planned_actions"][0]["actor"] == "philip"
    assert snapshot["planned_actions"][0]["kind"] == "groom_backlog"
    assert all(action["actor"] != "matthew" for action in snapshot["planned_actions"])


def test_planner_uses_priority_then_oldest_task_for_ready_replenishment(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    older = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc).isoformat()
    newer = datetime(2026, 4, 2, 8, 0, tzinfo=timezone.utc).isoformat()

    _write_task(
        workspace / "tasks" / "backlog" / "medium-older.md",
        "TASK-OLD",
        metadata={"acceptance_criteria": ["ship it"], "priority": "medium", "created_at": older},
    )
    _write_task(
        workspace / "tasks" / "backlog" / "high-newer.md",
        "TASK-HIGH",
        metadata={"acceptance_criteria": ["ship it"], "priority": "high", "created_at": newer},
    )

    snapshot = md.collect_meridian_snapshot(workspace)
    replenish = next(action for action in snapshot["planned_actions"] if action["kind"] == "replenish_ready")

    assert replenish["task_ids"][0] == "TASK-HIGH"

    (workspace / "tasks" / "backlog" / "high-newer.md").unlink()

    snapshot = md.collect_meridian_snapshot(workspace)
    replenish = next(action for action in snapshot["planned_actions"] if action["kind"] == "replenish_ready")
    assert replenish["task_ids"][0] == "TASK-OLD"


def test_planner_surfaces_stale_review_as_first_class_action(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    stale_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _write_task(
        workspace / "tasks" / "review" / "stale-review.md",
        "TASK-STALE",
        metadata={"updated_at": stale_time},
    )

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["stale_tasks"][0]["task_id"] == "TASK-STALE"
    assert snapshot["stale_tasks"][0]["queue"] == "review"
    assert snapshot["planned_actions"][0]["kind"] == "triage_stale_review"
    assert snapshot["planned_actions"][0]["actor"] == "matthew"


def test_planner_routes_request_changes_review_back_to_fatih(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "active" / "task-review.md", "TASK-REVIEW")
    _write_review_decision(
        workspace / "tasks" / "review" / "decisions" / "TASK-REVIEW-decision.md",
        review_task_id="TASK-REVIEW",
        review_outcome="request_changes",
    )

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["review_decision"]["review_outcome"] == "request_changes"
    assert snapshot["planned_actions"][0]["kind"] == "address_review_changes"
    assert snapshot["planned_actions"][0]["actor"] == "fatih"
    assert snapshot["planned_actions"][0]["reason"] == "review_decision_request_changes"


def test_planner_keeps_approved_review_with_matthew_for_finalization(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "active" / "task-review.md", "TASK-REVIEW")
    _write_review_decision(
        workspace / "tasks" / "review" / "decisions" / "TASK-REVIEW-decision.md",
        review_task_id="TASK-REVIEW",
        review_outcome="approved",
        decision_bucket="passed",
    )

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["planned_actions"][0]["kind"] == "finalize_review_approval"
    assert snapshot["planned_actions"][0]["actor"] == "matthew"
    assert snapshot["planned_actions"][0]["reason"] == "review_decision_approved"


def test_planner_marks_blocked_review_as_human_wait(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "review" / "active" / "task-review.md", "TASK-REVIEW")
    _write_review_decision(
        workspace / "tasks" / "review" / "decisions" / "TASK-REVIEW-decision.md",
        review_task_id="TASK-REVIEW",
        review_outcome="blocked",
    )

    snapshot = md.collect_meridian_snapshot(workspace)

    assert snapshot["planned_actions"][0]["kind"] == "await_human_review_resolution"
    assert snapshot["planned_actions"][0]["actor"] == "human"
    assert snapshot["planned_actions"][0]["dispatchable"] is False


def test_planner_respects_dependency_readiness_for_replenishment(tmp_path, monkeypatch):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(
        workspace / "tasks" / "backlog" / "blocked.md",
        "TASK-BLOCKED",
        metadata={"acceptance_criteria": ["ship it"], "depends_on": ["TASK-DEP"]},
    )
    _write_task(
        workspace / "tasks" / "backlog" / "ready.md",
        "TASK-READY",
        metadata={"acceptance_criteria": ["ship it"]},
    )

    snapshot = md.collect_meridian_snapshot(workspace)
    replenish = next(action for action in snapshot["planned_actions"] if action["kind"] == "replenish_ready")
    assert replenish["task_ids"] == ["TASK-READY"]


def test_meridian_command_status_prints_expected_fields(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    event_path = tmp_path / ".hermes" / "meridian" / "events.jsonl"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(md, "EVENT_LOG_PATH", event_path)

    _write_task(workspace / "tasks" / "backlog" / "backlog-task.md", "TASK-B")
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(
        json.dumps(
            {
                "id": "evt-1",
                "type": "task_transitioned",
                "at": "2026-04-05T12:00:00+00:00",
                "actor": "philip",
                "task_id": "TASK-B",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    rc = md.meridian_command(Namespace(meridian_command="status", workspace=str(workspace)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian status" in out
    assert "Active persona: philip" in out
    assert "Workflow state: backlog" in out
    assert "Waiting on:     philip" in out
    assert "backlog=1" in out
    assert "Next action:" in out
    assert "Why now:" in out
    assert "Recent events:" in out
    assert "task_transitioned" in out


def test_meridian_command_status_surfaces_maintenance_warning(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    _write_task(workspace / "tasks" / "backlog" / "backlog-task.md", "TASK-B")
    _write_task(workspace / "tasks" / "in-progress" / "legacy-task.md", "TASK-LEGACY")

    rc = md.meridian_command(Namespace(meridian_command="status", workspace=str(workspace)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Maintenance:" in out
    assert "issues detected" in out


def test_meridian_command_stale_prints_stale_tasks(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)

    stale_time = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc).isoformat()
    _write_task(
        workspace / "tasks" / "review" / "stale-task.md",
        "TASK-STALE",
        metadata={"updated_at": stale_time},
    )

    rc = md.meridian_command(Namespace(meridian_command="stale", workspace=str(workspace)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian stale tasks" in out
    assert "TASK-STALE" in out
    assert "review_sla_exceeded" in out


def test_meridian_command_leases_prints_worker_leases(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    md.dispatch_meridian(workspace, now=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc))
    rc = md.meridian_command(Namespace(meridian_command="leases", workspace=str(workspace)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian leases" in out
    assert "Worker leases:" in out
    assert "TASK-1" in out


def test_meridian_command_go_once_runs_single_pass(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    monkeypatch.setattr(md, "_local_policy_window", lambda now: "normal")
    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    rc = md.meridian_command(
        Namespace(
            meridian_command="go",
            workspace=str(workspace),
            sleep=0.0,
            idle_sleep=0.0,
            max_passes=None,
            once=True,
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian go loop" in out
    assert "Pass 1" in out
    assert "wake fatih for TASK-1" in out


def test_meridian_command_history_prints_workflow_history(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md
    from hermes_cli.meridian_workflow import claim_task, transition_task

    workspace = _make_workspace(tmp_path)
    state_path = tmp_path / ".hermes" / "meridian" / "workflow_state.json"
    monkeypatch.setattr(md, "STATE_PATH", state_path)
    _write_task(workspace / "tasks" / "ready" / "ready-task.md", "TASK-1")

    claim_task(workspace, task_id="TASK-1", actor="fatih")
    transition_task(workspace, task_id="TASK-1", actor="fatih", from_queue="ready", to_queue="in_progress")

    rc = md.meridian_command(
        Namespace(meridian_command="history", workspace=str(workspace), task_id="TASK-1")
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian task history" in out
    assert "TASK-1" in out
    assert "task_claimed" in out
    assert "task_transitioned" in out


def test_meridian_command_doctor_prints_report(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    _write_task(workspace / "tasks" / "in-progress" / "legacy-task.md", "TASK-LEGACY")

    rc = md.meridian_command(Namespace(meridian_command="doctor", workspace=str(workspace)))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian doctor" in out
    assert "legacy_in_progress=1" in out


def test_meridian_command_migrate_dry_run_prints_preview(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    _write_task(workspace / "tasks" / "in-progress" / "legacy-task.md", "TASK-LEGACY")

    rc = md.meridian_command(
        Namespace(meridian_command="migrate", workspace=str(workspace), apply=False)
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian in-progress migration" in out
    assert "would_move" in out
    assert (workspace / "tasks" / "in-progress" / "legacy-task.md").exists()


def test_meridian_command_migrate_review_prints_preview(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    _write_task(workspace / "tasks" / "review" / "task-1.md", "TASK-1")

    rc = md.meridian_command(
        Namespace(meridian_command="migrate-review", workspace=str(workspace), apply=False)
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian review migration" in out
    assert "category=active" in out
    assert "would_move" in out


def test_meridian_command_review_transition_prints_dry_run(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    _write_task(workspace / "tasks" / "review" / "task-1.md", "TASK-1")
    _write_review_decision(
        workspace / "tasks" / "review" / "decisions" / "TASK-1-decision.md",
        review_task_id="TASK-1",
        transition_recommendation={"from_queue": "review", "to_queue": "in_progress"},
    )

    rc = md.meridian_command(
        Namespace(meridian_command="review-transition", workspace=str(workspace), task_id="TASK-1", apply=False)
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Meridian review transition" in out
    assert "review -> in_progress" in out
    assert "Dry-run only" in out


def test_meridian_command_review_transition_apply_moves_task(tmp_path, monkeypatch, capsys):
    from hermes_cli import meridian_dispatcher as md

    workspace = _make_workspace(tmp_path)
    _write_task(workspace / "tasks" / "review" / "task-1.md", "TASK-1")
    _write_review_decision(
        workspace / "tasks" / "review" / "decisions" / "TASK-1-decision.md",
        review_task_id="TASK-1",
        review_outcome="approved",
        decision_bucket="passed",
        transition_recommendation={"from_queue": "review", "to_queue": "done"},
    )

    rc = md.meridian_command(
        Namespace(meridian_command="review-transition", workspace=str(workspace), task_id="TASK-1", apply=True)
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Transition applied" in out
    assert (workspace / "tasks" / "done" / "task-1.md").exists()


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


def test_main_routes_meridian_reconcile_subcommand(monkeypatch):
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
        ["hermes", "meridian", "reconcile", "--workspace", "/tmp/meridian-workspace"],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "reconcile",
        "workspace": "/tmp/meridian-workspace",
    }


def test_main_routes_meridian_go_subcommand(monkeypatch):
    import sys
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_meridian(args):
        captured["command"] = args.command
        captured["subcommand"] = args.meridian_command
        captured["workspace"] = args.workspace
        captured["sleep"] = args.sleep
        captured["idle_sleep"] = args.idle_sleep
        captured["max_passes"] = args.max_passes
        captured["once"] = args.once

    monkeypatch.setattr(main_mod, "cmd_meridian", fake_cmd_meridian)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "meridian",
            "go",
            "--workspace",
            "/tmp/meridian-workspace",
            "--sleep",
            "5",
            "--idle-sleep",
            "20",
            "--max-passes",
            "3",
            "--once",
        ],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "go",
        "workspace": "/tmp/meridian-workspace",
        "sleep": 5.0,
        "idle_sleep": 20.0,
        "max_passes": 3,
        "once": True,
    }


def test_main_routes_meridian_doctor_subcommand(monkeypatch):
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
        ["hermes", "meridian", "doctor", "--workspace", "/tmp/meridian-workspace"],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "doctor",
        "workspace": "/tmp/meridian-workspace",
    }


def test_main_routes_meridian_migrate_subcommand(monkeypatch):
    import sys
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_meridian(args):
        captured["command"] = args.command
        captured["subcommand"] = args.meridian_command
        captured["workspace"] = args.workspace
        captured["apply"] = args.apply

    monkeypatch.setattr(main_mod, "cmd_meridian", fake_cmd_meridian)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "meridian", "migrate", "--workspace", "/tmp/meridian-workspace", "--apply"],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "migrate",
        "workspace": "/tmp/meridian-workspace",
        "apply": True,
    }


def test_main_routes_meridian_migrate_review_subcommand(monkeypatch):
    import sys
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_meridian(args):
        captured["command"] = args.command
        captured["subcommand"] = args.meridian_command
        captured["workspace"] = args.workspace
        captured["apply"] = args.apply

    monkeypatch.setattr(main_mod, "cmd_meridian", fake_cmd_meridian)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "meridian", "migrate-review", "--workspace", "/tmp/meridian-workspace", "--apply"],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "migrate-review",
        "workspace": "/tmp/meridian-workspace",
        "apply": True,
    }


def test_main_routes_meridian_review_transition_subcommand(monkeypatch):
    import sys
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_meridian(args):
        captured["command"] = args.command
        captured["subcommand"] = args.meridian_command
        captured["workspace"] = args.workspace
        captured["task_id"] = args.task_id
        captured["apply"] = args.apply

    monkeypatch.setattr(main_mod, "cmd_meridian", fake_cmd_meridian)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes",
            "meridian",
            "review-transition",
            "TASK-1",
            "--workspace",
            "/tmp/meridian-workspace",
            "--apply",
        ],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "review-transition",
        "workspace": "/tmp/meridian-workspace",
        "task_id": "TASK-1",
        "apply": True,
    }


def test_main_routes_meridian_history_subcommand(monkeypatch):
    import sys
    import hermes_cli.main as main_mod

    captured = {}

    def fake_cmd_meridian(args):
        captured["command"] = args.command
        captured["subcommand"] = args.meridian_command
        captured["workspace"] = args.workspace
        captured["task_id"] = args.task_id

    monkeypatch.setattr(main_mod, "cmd_meridian", fake_cmd_meridian)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "meridian", "history", "TASK-42", "--workspace", "/tmp/meridian-workspace"],
    )

    main_mod.main()

    assert captured == {
        "command": "meridian",
        "subcommand": "history",
        "workspace": "/tmp/meridian-workspace",
        "task_id": "TASK-42",
    }
