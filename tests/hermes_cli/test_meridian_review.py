from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_cli.meridian_review import (
    MeridianReviewError,
    latest_review_decision,
    parse_review_decision,
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
