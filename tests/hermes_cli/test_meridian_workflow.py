from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from hermes_cli import meridian_runtime as mr
from hermes_cli.meridian_workflow import (
    HISTORY_KEY,
    MeridianWorkflowError,
    claim_task,
    locate_task,
    list_task_refs,
    transition_task,
)


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for queue in ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt"):
        (workspace / "tasks" / queue).mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture(autouse=True)
def _isolated_meridian_events(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "EVENT_LOG_PATH", tmp_path / ".hermes" / "meridian" / "events.jsonl")


def _write_task(
    path: Path,
    task_id: str,
    *,
    metadata: dict | None = None,
    body: str = "",
) -> None:
    data = {
        "id": task_id,
        "title": task_id,
        **(metadata or {}),
    }
    content = ["---", yaml.safe_dump(data, sort_keys=False).strip(), "---"]
    if body:
        content.extend(["", body.strip()])
    path.write_text("\n".join(content) + "\n", encoding="utf-8")


def test_claim_task_updates_frontmatter_and_history(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "ready" / "task-1.md"
    _write_task(task_path, "TASK-1")
    mr.EVENT_LOG_PATH = tmp_path / ".hermes" / "meridian" / "events.jsonl"

    result = claim_task(
        workspace,
        task_id="TASK-1",
        actor="fatih",
        lease_ttl=600,
        reason="starting implementation",
        now=datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc),
    )

    document = locate_task(workspace, "TASK-1")
    assert result["claimed_by"] == "fatih"
    assert result["status"] == "ready"
    assert document.metadata["claimed_by"] == "fatih"
    assert document.metadata["claimed_at"] == "2026-04-05T12:00:00+00:00"
    assert document.metadata["claim_expires_at"] == "2026-04-05T12:10:00+00:00"
    assert document.metadata["status"] == "ready"
    assert document.metadata[HISTORY_KEY][-1]["event"] == "task_claimed"
    event_lines = mr.EVENT_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert "task_claimed" in event_lines[0]


def test_claim_task_rejects_conflicting_owner(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "ready" / "task-1.md"
    _write_task(task_path, "TASK-1", metadata={"claimed_by": "fatih"})

    with pytest.raises(MeridianWorkflowError, match="already claimed by fatih"):
        claim_task(workspace, task_id="TASK-1", actor="matthew")


def test_claim_task_rejects_philip_in_ready_queue(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "ready" / "task-1.md"
    _write_task(task_path, "TASK-1")

    with pytest.raises(MeridianWorkflowError, match="Only fatih can claim tasks in ready"):
        claim_task(workspace, task_id="TASK-1", actor="philip")


def test_transition_ready_to_in_progress_requires_matching_claim(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "ready" / "task-1.md"
    _write_task(task_path, "TASK-1")

    with pytest.raises(MeridianWorkflowError, match="requires the task to be claimed"):
        transition_task(
            workspace,
            task_id="TASK-1",
            actor="fatih",
            from_queue="ready",
            to_queue="in_progress",
        )


def test_transition_moves_file_and_appends_history(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "ready" / "task-1.md"
    _write_task(task_path, "TASK-1")
    mr.EVENT_LOG_PATH = tmp_path / ".hermes" / "meridian" / "events.jsonl"
    claim_task(workspace, task_id="TASK-1", actor="fatih")

    result = transition_task(
        workspace,
        task_id="TASK-1",
        actor="fatih",
        from_queue="ready",
        to_queue="in_progress",
        notes="implementation started",
        now=datetime(2026, 4, 5, 12, 15, tzinfo=timezone.utc),
    )

    assert result["from_queue"] == "ready"
    assert result["to_queue"] == "in_progress"
    assert not task_path.exists()
    moved = workspace / "tasks" / "in_progress" / "task-1.md"
    assert moved.exists()
    document = locate_task(workspace, "TASK-1")
    assert document.metadata["status"] == "in_progress"
    assert document.metadata["last_transition_at"] == "2026-04-05T12:15:00+00:00"
    assert document.metadata[HISTORY_KEY][-1]["event"] == "task_transitioned"
    assert document.metadata[HISTORY_KEY][-1]["notes"] == "implementation started"
    event_lines = mr.EVENT_LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    assert any("task_transitioned" in line for line in event_lines)


def test_transition_rejects_philip_ready_to_in_progress_even_if_claimed(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "ready" / "task-1.md"
    _write_task(task_path, "TASK-1", metadata={"claimed_by": "philip"})

    with pytest.raises(MeridianWorkflowError, match="Only fatih can transition ready -> in_progress"):
        transition_task(
            workspace,
            task_id="TASK-1",
            actor="philip",
            from_queue="ready",
            to_queue="in_progress",
        )


def test_transition_rejects_invalid_queue_move(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "backlog" / "task-1.md"
    _write_task(task_path, "TASK-1")

    with pytest.raises(MeridianWorkflowError, match="Invalid transition: backlog -> review"):
        transition_task(
            workspace,
            task_id="TASK-1",
            actor="philip",
            from_queue="backlog",
            to_queue="review",
        )


def test_exceptional_reset_requires_reason(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "in_progress" / "task-1.md"
    _write_task(task_path, "TASK-1", metadata={"claimed_by": "fatih"})

    with pytest.raises(MeridianWorkflowError, match="requires a reason"):
        transition_task(
            workspace,
            task_id="TASK-1",
            actor="fatih",
            from_queue="in_progress",
            to_queue="backlog",
        )


def test_transition_to_waiting_human_sets_blocking_metadata(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "review" / "task-1.md"
    _write_task(task_path, "TASK-1", metadata={"claimed_by": "fatih"})

    transition_task(
        workspace,
        task_id="TASK-1",
        actor="matthew",
        from_queue="review",
        to_queue="waiting_human",
        reason="migration approval required",
    )

    moved = workspace / "tasks" / "waiting_human" / "task-1.md"
    document = locate_task(workspace, "TASK-1")
    assert moved.exists()
    assert document.metadata["status"] == "waiting_human"
    assert document.metadata["waiting_on"] == "human_confirmation"
    assert document.metadata["blocked_reason"] == "migration approval required"


def test_locate_task_prefers_canonical_in_progress_over_legacy_alias(tmp_path):
    workspace = _make_workspace(tmp_path)
    legacy_dir = workspace / "tasks" / "in-progress"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = legacy_dir / "task-1.md"
    canonical_path = workspace / "tasks" / "in_progress" / "task-1.md"
    _write_task(legacy_path, "TASK-1", metadata={"title": "legacy"})
    _write_task(canonical_path, "TASK-1", metadata={"title": "canonical"})

    document = locate_task(workspace, "TASK-1")

    assert document.path == canonical_path
    assert document.metadata["title"] == "canonical"


def test_list_task_refs_merges_legacy_in_progress_alias_without_duplicates(tmp_path):
    workspace = _make_workspace(tmp_path)
    legacy_dir = workspace / "tasks" / "in-progress"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    _write_task(legacy_dir / "legacy-only.md", "TASK-LEGACY")
    _write_task(workspace / "tasks" / "in_progress" / "canonical.md", "TASK-CANON")
    _write_task(legacy_dir / "duplicate.md", "TASK-DUP")
    _write_task(workspace / "tasks" / "in_progress" / "duplicate.md", "TASK-DUP")

    refs = list_task_refs(workspace)

    in_progress_ids = [item.task_id for item in refs["in_progress"]]
    assert in_progress_ids.count("TASK-DUP") == 1
    assert set(in_progress_ids) == {"TASK-LEGACY", "TASK-CANON", "TASK-DUP"}


def test_done_transition_clears_claim_metadata(tmp_path):
    workspace = _make_workspace(tmp_path)
    task_path = workspace / "tasks" / "review" / "task-1.md"
    _write_task(
        task_path,
        "TASK-1",
        metadata={
            "claimed_by": "fatih",
            "claimed_at": "2026-04-05T11:00:00+00:00",
            "claim_expires_at": "2026-04-05T12:00:00+00:00",
        },
    )

    transition_task(
        workspace,
        task_id="TASK-1",
        actor="matthew",
        from_queue="review",
        to_queue="done",
    )

    document = locate_task(workspace, "TASK-1")
    assert document.queue == "done"
    assert "claimed_by" not in document.metadata
    assert "claimed_at" not in document.metadata
    assert "claim_expires_at" not in document.metadata
