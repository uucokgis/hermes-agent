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
    if "meridian" not in text:
        return False

    # Direct intent markers for work requests in English/Turkish.
    work_markers = (
        "yap", "yapal", "hallet", "bak", "feature", "bug", "task", "review", "fix",
        "implement", "incele", "geliştir", "gelistir", "refactor",
        "philip", "fatih", "matthew",
    )
    return any(marker in text for marker in work_markers)


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
        "Follow the Meridian workflow skill below. Route new work through Philip "
        "first, use sequential handoff to Fatih and Matthew when task state makes "
        "it appropriate, and avoid polling or cron for immediate work.]"
    )
    runtime_note = (
        "This is a direct Meridian workflow request. Treat it as event-driven: "
        "wake the next persona only when task state requires it."
    )
    return _build_skill_message(
        loaded_skill,
        skill_dir,
        activation_note,
        user_instruction=user_message,
        runtime_note=runtime_note,
    )
