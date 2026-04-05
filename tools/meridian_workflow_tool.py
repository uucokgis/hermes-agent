"""Official Meridian workflow primitives."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.meridian_workflow import MeridianWorkflowError, claim_task, transition_task
from tools.registry import registry


TASK_CLAIM_SCHEMA = {
    "name": "task_claim",
    "description": "Claim a Meridian task through the official deterministic workflow API.",
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Workspace root that contains tasks/.",
            },
            "task_id": {
                "type": "string",
                "description": "Task id or filename to claim.",
            },
            "actor": {
                "type": "string",
                "description": "Actor claiming the task, such as philip, fatih, matthew, or human.",
            },
            "lease_ttl": {
                "type": "integer",
                "description": "Optional lease duration in seconds for bookkeeping.",
            },
            "reason": {
                "type": "string",
                "description": "Optional claim reason for the task history.",
            },
        },
        "required": ["task_id", "actor"],
        "additionalProperties": False,
    },
}

TASK_TRANSITION_SCHEMA = {
    "name": "task_transition",
    "description": "Transition a Meridian task between queues through the official deterministic workflow API.",
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Workspace root that contains tasks/.",
            },
            "task_id": {
                "type": "string",
                "description": "Task id or filename to transition.",
            },
            "actor": {
                "type": "string",
                "description": "Actor performing the transition.",
            },
            "from_queue": {
                "type": "string",
                "description": "Optional expected current queue for deterministic validation.",
            },
            "to_queue": {
                "type": "string",
                "description": "Destination queue name.",
            },
            "reason": {
                "type": "string",
                "description": "Transition reason, required for exceptional resets.",
            },
            "notes": {
                "type": "string",
                "description": "Optional notes to append to workflow history.",
            },
            "metadata_patch": {
                "type": "object",
                "description": "Optional metadata fields to merge into task frontmatter.",
            },
        },
        "required": ["task_id", "actor", "to_queue"],
        "additionalProperties": False,
    },
}


def _handle_task_claim(args: dict, **_: object) -> str:
    workspace = Path(args.get("workspace") or ".").resolve()
    try:
        result = claim_task(
            workspace,
            task_id=args.get("task_id", ""),
            actor=args.get("actor", ""),
            lease_ttl=args.get("lease_ttl"),
            reason=args.get("reason"),
        )
    except MeridianWorkflowError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    return json.dumps({"success": True, **result}, ensure_ascii=False)


def _handle_task_transition(args: dict, **_: object) -> str:
    workspace = Path(args.get("workspace") or ".").resolve()
    try:
        result = transition_task(
            workspace,
            task_id=args.get("task_id", ""),
            actor=args.get("actor", ""),
            from_queue=args.get("from_queue"),
            to_queue=args.get("to_queue", ""),
            reason=args.get("reason"),
            notes=args.get("notes"),
            metadata_patch=args.get("metadata_patch"),
        )
    except MeridianWorkflowError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    return json.dumps({"success": True, **result}, ensure_ascii=False)


registry.register(
    name="task_claim",
    toolset="meridian",
    schema=TASK_CLAIM_SCHEMA,
    handler=_handle_task_claim,
    emoji="🗂️",
)

registry.register(
    name="task_transition",
    toolset="meridian",
    schema=TASK_TRANSITION_SCHEMA,
    handler=_handle_task_transition,
    emoji="🔀",
)
