from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.meridian_quality import _local_review_candidates


def _write_review_file(path: Path, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(metadata, sort_keys=False).strip() + "\n---\n",
        encoding="utf-8",
    )


def test_local_review_candidates_prefer_active_and_skip_decision_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    _write_review_file(
        workspace / "tasks" / "review" / "active" / "task-active.md",
        {"id": "TASK-ACTIVE", "updated_at": "2026-04-08T01:00:00+00:00"},
    )
    _write_review_file(
        workspace / "tasks" / "review" / "TASK-ACTIVE-legacy.md",
        {"id": "TASK-ACTIVE", "updated_at": "2026-04-08T00:00:00+00:00"},
    )
    _write_review_file(
        workspace / "tasks" / "review" / "TASK-DECISION.md",
        {
            "review_schema_version": 1,
            "review_task_id": "TASK-ACTIVE",
            "review_kind": "decision",
            "review_outcome": "approved",
            "decision_bucket": "passed",
            "reviewer": "matthew",
            "status": "final",
            "required_actions": [],
            "updated_at": "2026-04-08T02:00:00+00:00",
        },
    )

    candidates = _local_review_candidates(str(workspace))

    assert candidates == [{"task_id": "TASK-ACTIVE", "transition_at": "2026-04-08T01:00:00+00:00"}]
