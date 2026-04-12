"""Automatic workflow overlays for direct user messages.

These helpers keep orchestration lightweight: no background service, no polling.
When a top-level user message clearly targets Meridian work, we inject the
Meridian workflow skill as a synthetic user overlay while preserving the
original user text for transcript/history persistence.
"""

from __future__ import annotations

from typing import Optional


def should_auto_route_meridian_message(user_message: str, *, delegate_depth: int = 0) -> bool:
    """Return True when a top-level message should load the Meridian workflow skill."""
    if delegate_depth > 0:
        return False
    text = (user_message or "").strip().lower()
    if not text:
        return False
    if text.startswith("/"):
        return False

    # Direct intent markers for work requests in English/Turkish.
    work_markers = (
        "yap", "yapal", "hallet", "bak", "feature", "bug", "task", "review", "fix",
        "implement", "incele", "geliştir", "gelistir", "refactor",
    )
    workflow_markers = ("philip", "fatih", "matthew")
    meridian_only_markers = ("branch", "checkout", "commit", "merge")
    queue_markers = ("backlog", "ready", "in_progress", "review", "done", "debt")

    if "meridian" in text:
        return any(marker in text for marker in (*work_markers, *workflow_markers, *meridian_only_markers))

    # Users often refer to the Meridian workflow by persona or queue name
    # without saying "Meridian" explicitly. Require a stronger combination of
    # workflow-specific language so ordinary "ready/review" phrasing does not
    # accidentally trigger the overlay.
    mentions_workflow = any(marker in text for marker in workflow_markers)
    mentions_queue = any(marker in text for marker in queue_markers)
    mentions_work = any(marker in text for marker in work_markers)
    return (mentions_workflow and (mentions_queue or mentions_work)) or (
        "backlog" in text and mentions_work
    )


def build_meridian_workflow_overlay(
    user_message: str,
    *,
    task_id: str | None = None,
) -> Optional[str]:
    """Return a skill overlay message for Meridian workflow requests, if available."""
    try:
        from agent.skill_commands import _build_skill_message, _load_skill_payload
    except Exception:
        return None

    loaded = _load_skill_payload("meridian/workflow", task_id=task_id)
    if not loaded:
        return None

    loaded_skill, skill_dir, _skill_name = loaded
    activation_note = (
        "[SYSTEM: The user is asking Hermes to handle Meridian work directly. "
        "Follow the Meridian workflow skill below. Use one agent end-to-end: "
        "shape the task if needed, create or switch to a task branch, implement "
        "the change, commit it, run a fresh reviewer-minded self-review in the "
        "Matthew lens, then push and merge only when the review passes. Keep "
        "using task_claim/task_transition as the canonical workflow contract.]"
    )
    runtime_note = (
        "This is a direct Meridian workflow request. Treat Philip, Fatih, and "
        "Matthew as working lenses inside one agent, not concurrent runtimes. "
        "Prefer deterministic task metadata, task branches, explicit review "
        "notes, and task_claim/task_transition over persona handoff or polling loops."
    )
    return _build_skill_message(
        loaded_skill,
        skill_dir,
        activation_note,
        user_instruction=user_message,
        runtime_note=runtime_note,
    )
