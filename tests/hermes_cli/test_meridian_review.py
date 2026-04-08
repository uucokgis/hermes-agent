from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli.meridian_review import (
    MeridianReviewError,
    apply_recommended_transition,
    format_review_transition_result,
    latest_review_decision,
    parse_review_decision,
    recommended_transition,
    review_detail_lines_for_task,
    review_brief_for_task,
)


def _write_review(path: Path, metadata: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ["---", yaml.safe_dump(metadata, sort_keys=False).strip(), "---"]
    if body:
        content.extend(["", body.strip()])
    path.write_text("\n".join(content) + "\n", encoding="utf-8")


def _decision_metadata(**overrides):
    data = {
        "review_schema_version": 1,
        "review_id": "REVIEW-20260408-001",
        "review_task_id": "TASK-1",
        "review_kind": "decision",
        "review_outcome": "request_changes",
        "decision_bucket": "blocking",
        "reviewer": "matthew",
        "status": "final",
        "required_actions": [
            {"id": "RA-1", "summary": "Fix selection loop", "status": "open"},
            {"id": "RA-2", "summary": "Add tests", "status": "done"},
        ],
        "updated_at": "2026-04-08T01:30:00+00:00",
        "summary": "Selection sync can recurse under rapid updates",
    }
    data.update(overrides)
    return data


def test_parse_review_decision_accepts_valid_envelope(tmp_path):
    path = tmp_path / "decision.md"
    _write_review(path, _decision_metadata())

    decision = parse_review_decision(path)

    assert decision.task_id == "TASK-1"
    assert decision.outcome == "request_changes"
    assert decision.bucket == "blocking"


def test_parse_review_decision_rejects_missing_required_fields(tmp_path):
    path = tmp_path / "decision.md"
    metadata = _decision_metadata()
    metadata.pop("required_actions")
    _write_review(path, metadata)

    with pytest.raises(MeridianReviewError, match="missing required fields"):
        parse_review_decision(path)


def test_latest_review_decision_prefers_final_and_newer_timestamp(tmp_path):
    workspace = tmp_path / "workspace"
    review_dir = workspace / "tasks" / "review"
    _write_review(
        review_dir / "TASK-1-older.md",
        _decision_metadata(review_id="REVIEW-1", updated_at="2026-04-08T01:00:00+00:00"),
    )
    _write_review(
        review_dir / "TASK-1-draft.md",
        _decision_metadata(review_id="REVIEW-2", status="draft", updated_at="2026-04-08T03:00:00+00:00"),
    )
    _write_review(
        review_dir / "TASK-1-final.md",
        _decision_metadata(review_id="REVIEW-3", updated_at="2026-04-08T02:00:00+00:00"),
    )

    decision = latest_review_decision("TASK-1", workspace)

    assert decision is not None
    assert decision.metadata["review_id"] == "REVIEW-3"


def test_review_brief_for_task_includes_open_action_count(tmp_path):
    workspace = tmp_path / "workspace"
    review_dir = workspace / "tasks" / "review" / "decisions"
    _write_review(review_dir / "TASK-1-decision.md", _decision_metadata())

    brief = review_brief_for_task("TASK-1", workspace)

    assert "request_changes" in brief
    assert "blocking" in brief
    assert "open_actions: `1`" in brief


def test_latest_review_decision_reads_decisions_subdirectory_first(tmp_path):
    workspace = tmp_path / "workspace"
    flat_review_dir = workspace / "tasks" / "review"
    decisions_dir = flat_review_dir / "decisions"
    _write_review(
        flat_review_dir / "TASK-1-legacy.md",
        _decision_metadata(review_id="REVIEW-LEGACY", updated_at="2026-04-08T01:00:00+00:00"),
    )
    _write_review(
        decisions_dir / "TASK-1-current.md",
        _decision_metadata(review_id="REVIEW-CURRENT", updated_at="2026-04-08T02:00:00+00:00"),
    )

    decision = latest_review_decision("TASK-1", workspace)

    assert decision is not None
    assert decision.metadata["review_id"] == "REVIEW-CURRENT"


def test_parse_review_decision_validates_transition_recommendation(tmp_path):
    path = tmp_path / "decision.md"
    _write_review(
        path,
        _decision_metadata(
            transition_recommendation={"from_queue": "review", "to_queue": "not-a-queue"},
        ),
    )

    with pytest.raises(MeridianReviewError, match="Invalid transition_recommendation.to_queue"):
        parse_review_decision(path)


def test_review_detail_lines_include_transition_and_open_actions(tmp_path):
    workspace = tmp_path / "workspace"
    review_dir = workspace / "tasks" / "review" / "decisions"
    _write_review(
        review_dir / "TASK-1-decision.md",
        _decision_metadata(
            transition_recommendation={"from_queue": "review", "to_queue": "in_progress"},
            required_actions=[
                {"id": "RA-1", "summary": "Fix regression", "severity": "blocking", "owner": "fatih", "status": "open"},
                {"id": "RA-2", "summary": "Add regression test", "severity": "review", "owner": "fatih", "status": "open"},
                {"id": "RA-3", "summary": "Document edge case", "severity": "advisory", "owner": "philip", "status": "open"},
                {"id": "RA-4", "summary": "Already done", "severity": "review", "owner": "fatih", "status": "done"},
            ],
        ),
    )

    lines = review_detail_lines_for_task("TASK-1", workspace, max_actions=2)

    assert lines[0] == "Recommended transition: `review -> in_progress`"
    assert any("Fix regression" in line for line in lines)
    assert any("Add regression test" in line for line in lines)
    assert any("+1 more open review action(s)" in line for line in lines)


def test_recommended_transition_infers_default_route_from_final_outcome(tmp_path):
    workspace = tmp_path / "workspace"
    review_dir = workspace / "tasks" / "review" / "decisions"
    _write_review(review_dir / "TASK-1-decision.md", _decision_metadata(review_outcome="approved"))

    transition = recommended_transition("TASK-1", workspace)

    assert transition is not None
    assert transition["from_queue"] == "review"
    assert transition["to_queue"] == "done"
    assert transition["source"] == "inferred"


def test_apply_recommended_transition_dry_run_reports_ready(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "tasks" / "review").mkdir(parents=True, exist_ok=True)
    _write_review(
        workspace / "tasks" / "review" / "task-1.md",
        {"id": "TASK-1", "title": "TASK-1"},
    )
    _write_review(
        workspace / "tasks" / "review" / "decisions" / "TASK-1-decision.md",
        _decision_metadata(
            transition_recommendation={"from_queue": "review", "to_queue": "in_progress"},
            reviewer="matthew",
        ),
    )

    result = apply_recommended_transition("TASK-1", workspace, apply=False)

    assert result["status"] == "ready"
    assert result["actor"] == "matthew"
    assert result["to_queue"] == "in_progress"


def test_apply_recommended_transition_moves_task_when_apply_enabled(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "tasks" / "review").mkdir(parents=True, exist_ok=True)
    _write_review(
        workspace / "tasks" / "review" / "task-1.md",
        {"id": "TASK-1", "title": "TASK-1"},
    )
    _write_review(
        workspace / "tasks" / "review" / "decisions" / "TASK-1-decision.md",
        _decision_metadata(
            transition_recommendation={"from_queue": "review", "to_queue": "waiting_human"},
            review_outcome="needs_human",
            reviewer="matthew",
        ),
    )

    result = apply_recommended_transition("TASK-1", workspace, apply=True)

    assert result["status"] == "applied"
    assert (workspace / "tasks" / "waiting_human" / "task-1.md").exists()


def test_apply_recommended_transition_reports_queue_mismatch(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "tasks" / "done").mkdir(parents=True, exist_ok=True)
    _write_review(
        workspace / "tasks" / "done" / "task-1.md",
        {"id": "TASK-1", "title": "TASK-1"},
    )
    _write_review(
        workspace / "tasks" / "review" / "decisions" / "TASK-1-decision.md",
        _decision_metadata(
            transition_recommendation={"from_queue": "review", "to_queue": "done"},
            review_outcome="approved",
        ),
    )

    result = apply_recommended_transition("TASK-1", workspace, apply=True)

    assert result["status"] == "queue_mismatch"
    assert result["current_queue"] == "done"


def test_format_review_transition_result_includes_dry_run_note(tmp_path):
    workspace = tmp_path / "workspace"
    review_dir = workspace / "tasks" / "review" / "decisions"
    _write_review(
        review_dir / "TASK-1-decision.md",
        _decision_metadata(transition_recommendation={"from_queue": "review", "to_queue": "in_progress"}),
    )

    result = format_review_transition_result(
        {
            **recommended_transition("TASK-1", workspace),
            "status": "ready",
            "current_queue": "review",
        }
    )

    assert "Dry-run only" in result
