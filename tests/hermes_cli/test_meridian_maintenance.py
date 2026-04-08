from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.meridian_maintenance import (
    classify_review_artifact,
    meridian_doctor_report,
    migrate_in_progress_queue,
)


def _write_task(path: Path, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(metadata, sort_keys=False).strip() + "\n---\n",
        encoding="utf-8",
    )


def test_doctor_report_detects_legacy_and_flat_review_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    legacy = workspace / "tasks" / "in-progress"
    canonical = workspace / "tasks" / "in_progress"
    review = workspace / "tasks" / "review"
    legacy.mkdir(parents=True, exist_ok=True)
    canonical.mkdir(parents=True, exist_ok=True)
    review.mkdir(parents=True, exist_ok=True)

    _write_task(legacy / "legacy-only.md", {"id": "TASK-LEGACY"})
    _write_task(legacy / "dup.md", {"id": "TASK-DUP", "title": "legacy"})
    _write_task(canonical / "dup.md", {"id": "TASK-DUP", "title": "canonical"})
    _write_task(
        review / "TASK-DECISION.md",
        {
            "review_schema_version": 1,
            "review_task_id": "TASK-1",
            "review_kind": "decision",
            "review_outcome": "approved",
            "decision_bucket": "passed",
            "reviewer": "matthew",
            "status": "final",
            "required_actions": [],
            "updated_at": "2026-04-08T02:00:00+00:00",
        },
    )
    _write_task(review / "TASK-PATROL.md", {"review_kind": "patrol", "status": "final"})

    report = meridian_doctor_report(workspace)

    assert report["healthy"] is False
    assert {item["state"] for item in report["legacy_in_progress"]} == {"legacy_only", "divergent_duplicate"}
    assert report["flat_review_counts"]["decision"] == 1
    assert report["flat_review_counts"]["patrol"] == 1


def test_classify_review_artifact_uses_filename_heuristics(tmp_path):
    path = tmp_path / "review" / "TASK-123-REQUEST-CHANGES.md"
    _write_task(path, {"id": "TASK-123"})

    assert classify_review_artifact(path) == "decision"


def test_migrate_in_progress_queue_moves_and_cleans_duplicates(tmp_path):
    workspace = tmp_path / "workspace"
    legacy = workspace / "tasks" / "in-progress"
    canonical = workspace / "tasks" / "in_progress"
    legacy.mkdir(parents=True, exist_ok=True)
    canonical.mkdir(parents=True, exist_ok=True)

    _write_task(legacy / "legacy-only.md", {"id": "TASK-LEGACY"})
    _write_task(legacy / "dup.md", {"id": "TASK-DUP", "title": "same"})
    _write_task(canonical / "dup.md", {"id": "TASK-DUP", "title": "same"})
    _write_task(legacy / "divergent.md", {"id": "TASK-DIVERGENT", "title": "legacy"})
    _write_task(canonical / "divergent.md", {"id": "TASK-DIVERGENT", "title": "canonical"})

    dry_run = migrate_in_progress_queue(workspace, apply=False)

    assert {item["status"] for item in dry_run["items"]} == {
        "would_move",
        "would_remove_duplicate",
        "blocked_divergent",
    }
    assert (legacy / "legacy-only.md").exists()

    applied = migrate_in_progress_queue(workspace, apply=True)

    assert (canonical / "legacy-only.md").exists()
    assert not (legacy / "legacy-only.md").exists()
    assert not (legacy / "dup.md").exists()
    assert (legacy / "divergent.md").exists()
    assert (legacy / "README.md").exists()
    assert applied["summary"]["moved"] == 1
    assert applied["summary"]["removed_duplicates"] == 1
    assert applied["summary"]["blocked"] == 1
