"""Structured Meridian review decision helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from hermes_cli.meridian_runtime import parse_iso_datetime


REVIEW_SCHEMA_VERSION = 1
REVIEW_REQUIRED_FIELDS = (
    "review_schema_version",
    "review_task_id",
    "review_kind",
    "review_outcome",
    "decision_bucket",
    "reviewer",
    "status",
    "required_actions",
    "updated_at",
)
REVIEW_KIND_VALUES = frozenset({"decision", "patrol", "triage"})
REVIEW_OUTCOME_VALUES = frozenset({"approved", "request_changes", "blocked", "debt_only", "needs_human"})
DECISION_BUCKET_VALUES = frozenset({"passed", "advisory", "review", "blocking"})
REVIEW_STATUS_VALUES = frozenset({"draft", "final", "superseded", "archived"})
TRANSITION_QUEUE_VALUES = frozenset({"backlog", "ready", "in_progress", "review", "waiting_human", "done", "debt"})
DECISION_FILENAMES = (
    "APPROVAL",
    "REQUEST-CHANGES",
    "REQUEST_CHANGES",
    "DECISION",
    "REVIEW",
)


class MeridianReviewError(ValueError):
    """Raised when a structured review envelope is malformed."""


@dataclass(frozen=True)
class MeridianReviewDecision:
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def task_id(self) -> str:
        return str(self.metadata["review_task_id"])

    @property
    def outcome(self) -> str:
        return str(self.metadata["review_outcome"])

    @property
    def bucket(self) -> str:
        return str(self.metadata["decision_bucket"])

    @property
    def status(self) -> str:
        return str(self.metadata["status"])

    @property
    def updated_at(self) -> str:
        return str(self.metadata["updated_at"])

    @property
    def required_actions(self) -> list[dict[str, Any]]:
        return [item for item in self.metadata.get("required_actions", []) if isinstance(item, dict)]

    @property
    def open_actions(self) -> list[dict[str, Any]]:
        return [
            item for item in self.required_actions
            if str(item.get("status") or "open").strip().lower() not in {"done", "closed"}
        ]

    @property
    def transition_recommendation(self) -> dict[str, Any]:
        value = self.metadata.get("transition_recommendation")
        return value if isinstance(value, dict) else {}


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if content.startswith("---\n"):
        closing = content.find("\n---\n", 4)
        if closing != -1:
            raw = content[4:closing]
            body = content[closing + 5 :]
            parsed = yaml.safe_load(raw) or {}
            return dict(parsed) if isinstance(parsed, dict) else {}, body.lstrip("\n")
    return {}, content


def _review_candidate_dirs(workspace: Path) -> tuple[Path, ...]:
    review_root = workspace / "tasks" / "review"
    return (
        review_root / "decisions",
        review_root,
    )


def is_review_decision_artifact(path: Path, metadata: dict[str, Any]) -> bool:
    kind = str(metadata.get("review_kind") or "").strip().lower()
    if kind:
        return kind == "decision"
    name = path.name.upper()
    return any(marker in name for marker in DECISION_FILENAMES)


def parse_review_decision(path: str | Path) -> MeridianReviewDecision:
    review_path = Path(path)
    try:
        content = review_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MeridianReviewError(f"Unable to read review artifact: {review_path}") from exc
    metadata, body = _split_frontmatter(content)
    if not metadata:
        raise MeridianReviewError(f"Missing structured review envelope: {review_path}")

    missing = [field for field in REVIEW_REQUIRED_FIELDS if field not in metadata]
    if missing:
        raise MeridianReviewError(f"Review envelope missing required fields {missing}: {review_path}")
    if metadata.get("review_schema_version") != REVIEW_SCHEMA_VERSION:
        raise MeridianReviewError(f"Unsupported review schema version in {review_path}")
    if str(metadata.get("review_kind")).strip().lower() not in REVIEW_KIND_VALUES:
        raise MeridianReviewError(f"Invalid review_kind in {review_path}")
    if str(metadata.get("review_outcome")).strip().lower() not in REVIEW_OUTCOME_VALUES:
        raise MeridianReviewError(f"Invalid review_outcome in {review_path}")
    if str(metadata.get("decision_bucket")).strip().lower() not in DECISION_BUCKET_VALUES:
        raise MeridianReviewError(f"Invalid decision_bucket in {review_path}")
    if str(metadata.get("status")).strip().lower() not in REVIEW_STATUS_VALUES:
        raise MeridianReviewError(f"Invalid review status in {review_path}")
    if not isinstance(metadata.get("required_actions"), list):
        raise MeridianReviewError(f"required_actions must be a list in {review_path}")
    for index, item in enumerate(metadata.get("required_actions") or []):
        if not isinstance(item, dict):
            raise MeridianReviewError(f"required_actions[{index}] must be a mapping in {review_path}")
        if not str(item.get("summary") or "").strip():
            raise MeridianReviewError(f"required_actions[{index}] is missing summary in {review_path}")
    transition = metadata.get("transition_recommendation")
    if transition is not None:
        if not isinstance(transition, dict):
            raise MeridianReviewError(f"transition_recommendation must be a mapping in {review_path}")
        from_queue = str(transition.get("from_queue") or "").strip()
        to_queue = str(transition.get("to_queue") or "").strip()
        if from_queue and from_queue not in TRANSITION_QUEUE_VALUES:
            raise MeridianReviewError(f"Invalid transition_recommendation.from_queue in {review_path}")
        if to_queue and to_queue not in TRANSITION_QUEUE_VALUES:
            raise MeridianReviewError(f"Invalid transition_recommendation.to_queue in {review_path}")
    updated_at = metadata.get("updated_at")
    if isinstance(updated_at, datetime):
        metadata["updated_at"] = updated_at.isoformat()
    if parse_iso_datetime(metadata.get("updated_at")) is None:
        raise MeridianReviewError(f"Invalid updated_at in {review_path}")

    return MeridianReviewDecision(path=review_path, metadata=metadata, body=body)


def latest_review_decision(task_id: str, workspace: str | Path | None) -> MeridianReviewDecision | None:
    normalized_task_id = str(task_id).strip()
    if not normalized_task_id:
        return None
    workspace_path = Path(workspace or ".").resolve()
    candidates: list[MeridianReviewDecision] = []
    seen_paths: set[Path] = set()
    for directory in _review_candidate_dirs(workspace_path):
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                metadata, _body = _split_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if not is_review_decision_artifact(path, metadata):
                continue
            if str(metadata.get("review_task_id") or "").strip() != normalized_task_id:
                continue
            try:
                decision = parse_review_decision(path)
            except MeridianReviewError:
                continue
            candidates.append(decision)
    if not candidates:
        return None

    def _sort_key(item: MeridianReviewDecision) -> tuple[int, Any, str]:
        status_rank = 0 if item.status == "final" else 1
        parsed = parse_iso_datetime(item.updated_at)
        return (status_rank, -(parsed.timestamp() if parsed else 0.0), item.path.name)

    return sorted(candidates, key=_sort_key)[0]


def review_brief_for_task(task_id: str, workspace: str | Path | None) -> str:
    decision = latest_review_decision(task_id, workspace)
    if not decision:
        return ""
    open_actions = len(decision.open_actions)
    summary = str(decision.metadata.get("summary") or "").strip()
    summary_part = f" | {summary}" if summary else ""
    return (
        f"Review decision: `{decision.outcome}`"
        f" | bucket: `{decision.bucket}`"
        f" | status: `{decision.status}`"
        f" | open_actions: `{open_actions}`"
        f"{summary_part}"
    )


def review_detail_lines_for_task(task_id: str, workspace: str | Path | None, *, max_actions: int = 3) -> list[str]:
    decision = latest_review_decision(task_id, workspace)
    if not decision:
        return []
    lines: list[str] = []
    transition = decision.transition_recommendation
    from_queue = str(transition.get("from_queue") or "").strip()
    to_queue = str(transition.get("to_queue") or "").strip()
    if from_queue or to_queue:
        transition_text = f"{from_queue or '?'} -> {to_queue or '?'}"
        lines.append(f"Recommended transition: `{transition_text}`")
    for action in decision.open_actions[:max_actions]:
        severity = str(action.get("severity") or "open").strip()
        summary = str(action.get("summary") or "").strip()
        owner = str(action.get("owner") or "").strip()
        owner_part = f" | owner: `{owner}`" if owner else ""
        lines.append(f"- [{severity}] {summary}{owner_part}")
    remaining = len(decision.open_actions) - max_actions
    if remaining > 0:
        lines.append(f"- +{remaining} more open review action(s)")
    return lines
