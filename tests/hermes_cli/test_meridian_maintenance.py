from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.meridian_maintenance import (
    classify_review_artifact,
    format_review_migration,
    meridian_doctor_report,
    migrate_in_progress_queue,
    migrate_review_queue,
    run_meridian_doctor,
    run_migrate_in_progress_queue,
    run_migrate_review_queue,
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


def test_classify_review_artifact_maps_summary_and_specs_to_archive(tmp_path):
    summary = tmp_path / "review" / "MATTHEW-20260407-FINAL-SUMMARY.md"
    spec_file = tmp_path / "review" / "map-selection.spec.ts"
    readme = tmp_path / "review" / "README.md"
    _write_task(summary, {})
    _write_task(spec_file, {})
    _write_task(readme, {})

    assert classify_review_artifact(summary) == "archive"
    assert classify_review_artifact(spec_file) == "archive"
    assert classify_review_artifact(readme) == "archive"


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


def test_migrate_review_queue_classifies_and_moves_flat_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    review = workspace / "tasks" / "review"
    review.mkdir(parents=True, exist_ok=True)

    _write_task(review / "active-task.md", {"id": "TASK-ACTIVE"})
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

    dry_run = migrate_review_queue(workspace, apply=False)

    assert {item["category"] for item in dry_run["items"]} == {"active", "decision", "patrol"}
    assert {item["status"] for item in dry_run["items"]} == {"would_move"}

    applied = migrate_review_queue(workspace, apply=True)

    assert (workspace / "tasks" / "review" / "active" / "active-task.md").exists()
    assert (workspace / "tasks" / "review" / "decisions" / "TASK-DECISION.md").exists()
    assert (workspace / "tasks" / "review" / "patrol" / "TASK-PATROL.md").exists()
    assert applied["summary"]["moved"] == 3


def test_migrate_review_queue_removes_identical_duplicates_and_blocks_divergent(tmp_path):
    workspace = tmp_path / "workspace"
    review = workspace / "tasks" / "review"
    decisions = review / "decisions"
    review.mkdir(parents=True, exist_ok=True)
    decisions.mkdir(parents=True, exist_ok=True)

    decision_metadata = {
        "review_schema_version": 1,
        "review_task_id": "TASK-1",
        "review_kind": "decision",
        "review_outcome": "approved",
        "decision_bucket": "passed",
        "reviewer": "matthew",
        "status": "final",
        "required_actions": [],
        "updated_at": "2026-04-08T02:00:00+00:00",
    }
    _write_task(review / "dup.md", decision_metadata)
    _write_task(decisions / "dup.md", decision_metadata)
    _write_task(review / "divergent.md", {**decision_metadata, "summary": "legacy"})
    _write_task(decisions / "divergent.md", {**decision_metadata, "summary": "canonical"})

    applied = migrate_review_queue(workspace, apply=True)

    assert not (review / "dup.md").exists()
    assert (review / "divergent.md").exists()
    assert applied["summary"]["removed_duplicates"] == 1
    assert applied["summary"]["blocked"] == 1


def test_format_review_migration_handles_empty_report(tmp_path):
    workspace = tmp_path / "workspace"
    report = migrate_review_queue(workspace, apply=False)

    rendered = format_review_migration(report)

    assert "No flat review artifacts found." in rendered


def test_run_meridian_doctor_uses_remote_target_when_local_missing(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "hermes_cli.meridian_maintenance._resolve_maintenance_target",
        lambda workspace: type(
            "_Target",
            (),
            {"mode": "ssh", "workspace": "/home/umut/meridian", "host": "192.168.1.107", "user": "umut"},
        )(),
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_maintenance._remote_doctor_report",
        lambda target: captured.setdefault("report", {"workspace": target.workspace, "healthy": True}),
    )

    report = run_meridian_doctor("/home/umut/meridian")

    assert report["workspace"] == "/home/umut/meridian"
    assert report["healthy"] is True


def test_run_migrations_use_remote_target_when_local_missing(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.meridian_maintenance._resolve_maintenance_target",
        lambda workspace: type(
            "_Target",
            (),
            {"mode": "ssh", "workspace": "/home/umut/meridian", "host": "192.168.1.107", "user": "umut"},
        )(),
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_maintenance._remote_migrate_in_progress",
        lambda target, apply=False: {"workspace": target.workspace, "apply": apply, "items": [], "summary": {}},
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_maintenance._remote_migrate_review",
        lambda target, apply=False: {"workspace": target.workspace, "apply": apply, "items": [], "summary": {}},
    )

    in_progress = run_migrate_in_progress_queue("/home/umut/meridian", apply=True)
    review = run_migrate_review_queue("/home/umut/meridian", apply=False)

    assert in_progress["workspace"] == "/home/umut/meridian"
    assert in_progress["apply"] is True
    assert review["workspace"] == "/home/umut/meridian"
    assert review["apply"] is False
