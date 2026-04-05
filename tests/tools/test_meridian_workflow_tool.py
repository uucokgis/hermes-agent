import json
from pathlib import Path

import pytest
import yaml

from hermes_cli import meridian_runtime as mr
from tools.meridian_workflow_tool import _handle_task_claim, _handle_task_transition


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    for queue in ("backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt"):
        (workspace / "tasks" / queue).mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture(autouse=True)
def _isolated_meridian_events(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "EVENT_LOG_PATH", tmp_path / ".hermes" / "meridian" / "events.jsonl")


def _write_task(path: Path, task_id: str) -> None:
    payload = {
        "id": task_id,
        "title": task_id,
    }
    path.write_text(
        "---\n" + yaml.safe_dump(payload, sort_keys=False).strip() + "\n---\n",
        encoding="utf-8",
    )


def test_task_claim_tool_returns_success_payload(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_task(workspace / "tasks" / "ready" / "task-1.md", "TASK-1")

    result = json.loads(
        _handle_task_claim(
            {
                "workspace": str(workspace),
                "task_id": "TASK-1",
                "actor": "fatih",
                "lease_ttl": 300,
            }
        )
    )

    assert result["success"] is True
    assert result["claimed_by"] == "fatih"


def test_task_transition_tool_returns_validation_errors(tmp_path):
    workspace = _make_workspace(tmp_path)
    _write_task(workspace / "tasks" / "backlog" / "task-1.md", "TASK-1")

    result = json.loads(
        _handle_task_transition(
            {
                "workspace": str(workspace),
                "task_id": "TASK-1",
                "actor": "philip",
                "from_queue": "backlog",
                "to_queue": "review",
            }
        )
    )

    assert result["success"] is False
    assert "Invalid transition" in result["error"]
