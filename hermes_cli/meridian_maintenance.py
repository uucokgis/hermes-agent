"""Meridian maintenance helpers for queue migration and drift detection."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_cli.meridian_review import is_review_decision_artifact
from hermes_cli.meridian_workflow import (
    MeridianWorkflowError,
    _split_task_document,
    canonical_queue_dir,
    workspace_root_from_task_path,
)


REVIEW_CATEGORY_ORDER = ("active", "decision", "patrol", "archive", "unknown")
REVIEW_CATEGORY_DIRECTORIES = {
    "active": "active",
    "decision": "decisions",
    "patrol": "patrol",
    "archive": "archive",
}
DECISION_FILENAME_MARKERS = (
    "APPROVAL",
    "APPROVED",
    "REQUEST-CHANGES",
    "REQUEST_CHANGES",
    "DECISION",
    "REVIEW",
    "FINDINGS",
)
ARCHIVE_FILENAME_MARKERS = (
    "SUMMARY",
    "STATUS",
    "UPDATE",
    "TRANSITION",
    "COMPLETION",
    "COMPLETE",
    "COMPLETED",
    "IMPLEMENTED",
    "IMPLEMENTATION_COMPLETE",
    "IMPLEMENTATION_SUMMARY",
)


@dataclass(frozen=True)
class MaintenanceTarget:
    mode: str
    workspace: str
    host: str | None = None
    user: str | None = None
    key: str | None = None
    password: str | None = None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _task_metadata(path: Path) -> dict[str, Any]:
    metadata, _body = _split_task_document(_read_text(path))
    return metadata if isinstance(metadata, dict) else {}


def _task_identity(path: Path) -> tuple[str, str]:
    metadata = _task_metadata(path)
    return str(metadata.get("id") or path.stem), path.name


def _maintenance_ssh_settings(explicit_workspace: str | None = None) -> dict[str, str] | None:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        config = {}
    terminal = config.get("terminal") or {}
    backend = str(terminal.get("backend") or "").strip().lower()
    env_host = str(os.getenv("HERMES_MERIDIAN_QUALITY_SSH_HOST") or os.getenv("TERMINAL_SSH_HOST") or "").strip()
    env_user = str(os.getenv("HERMES_MERIDIAN_QUALITY_SSH_USER") or os.getenv("TERMINAL_SSH_USER") or "").strip()
    env_key = str(os.getenv("HERMES_MERIDIAN_QUALITY_SSH_KEY") or os.getenv("TERMINAL_SSH_KEY") or "").strip()
    env_password = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_PASSWORD")
        or os.getenv("TERMINAL_SSH_PASSWORD")
        or ""
    ).strip()
    env_workspace = str(
        explicit_workspace
        or os.getenv("HERMES_MERIDIAN_QUALITY_WORKSPACE")
        or os.getenv("HERMES_MERIDIAN_WORKSPACE")
        or ""
    ).strip()
    host = env_host or str(terminal.get("ssh_host") or "").strip()
    user = env_user or str(terminal.get("ssh_user") or "").strip()
    key = env_key or str(terminal.get("ssh_key") or "").strip()
    workspace = env_workspace or str(terminal.get("cwd") or "").strip()
    if (backend == "ssh" or host) and host and user:
        return {
            "host": host,
            "user": user,
            "key": key,
            "password": env_password,
            "workspace": workspace or str(Path.home() / "Meridian"),
        }
    return None


def _resolve_maintenance_target(workspace: str | Path) -> MaintenanceTarget:
    workspace_path = Path(workspace).expanduser()
    if workspace_path.exists():
        return MaintenanceTarget(mode="local", workspace=str(workspace_path.resolve()))
    ssh = _maintenance_ssh_settings(str(workspace))
    if ssh:
        return MaintenanceTarget(
            mode="ssh",
            workspace=ssh["workspace"],
            host=ssh["host"],
            user=ssh["user"],
            key=ssh.get("key") or None,
            password=ssh.get("password") or None,
        )
    raise FileNotFoundError(f"Meridian workspace does not exist and no SSH maintenance target is configured: {workspace}")


def _run_ssh(target: MaintenanceTarget, command: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    remote_command = f"cd {shlex.quote(target.workspace)} && {command}"
    if target.password:
        cmd = [
            "sshpass",
            "-e",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "PreferredAuthentications=password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "ConnectTimeout=8",
            f"{target.user}@{target.host}",
            remote_command,
        ]
        env = os.environ.copy()
        env["SSHPASS"] = target.password
    else:
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=8",
        ]
        if target.key:
            cmd.extend(["-i", target.key])
        cmd.extend([f"{target.user}@{target.host}", remote_command])
        env = None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env)


def _remote_frontmatter_script_body() -> str:
    return """
from pathlib import Path

REVIEW_CATEGORY_DIRECTORIES = {
    "active": "active",
    "decision": "decisions",
    "patrol": "patrol",
    "archive": "archive",
}
DECISION_FILENAME_MARKERS = (
    "APPROVAL",
    "APPROVED",
    "REQUEST-CHANGES",
    "REQUEST_CHANGES",
    "DECISION",
    "REVIEW",
    "FINDINGS",
)
ARCHIVE_FILENAME_MARKERS = (
    "SUMMARY",
    "STATUS",
    "UPDATE",
    "TRANSITION",
    "COMPLETION",
    "COMPLETE",
    "COMPLETED",
    "IMPLEMENTED",
    "IMPLEMENTATION_COMPLETE",
    "IMPLEMENTATION_SUMMARY",
)

def read_text(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""

def parse_frontmatter(text):
    if not text.startswith("---\\n"):
        return {}
    closing = text.find("\\n---\\n", 4)
    if closing == -1:
        return {}
    metadata = {}
    for raw in text[4:closing].splitlines():
        if not raw or raw.startswith((" ", "-")) or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        metadata[key.strip()] = value.strip().strip("'\\\"")
    return metadata

def task_metadata(path):
    return parse_frontmatter(read_text(path))

def task_identity(path):
    metadata = task_metadata(path)
    return str(metadata.get("id") or path.stem), path.name

def compare_duplicate_state(left, right):
    return "identical_duplicate" if read_text(left) == read_text(right) else "divergent_duplicate"

def classify_review_artifact(path):
    metadata = task_metadata(path)
    name = path.name.upper()
    status = str(metadata.get("status") or "").strip().lower()
    review_kind = str(metadata.get("review_kind") or "").strip().lower()
    suffix = "".join(path.suffixes).lower()
    if review_kind:
        if review_kind == "decision":
            return "decision"
        if review_kind == "patrol":
            return "patrol"
    if any(fragment in name for fragment in DECISION_FILENAME_MARKERS):
        return "decision"
    if status in {"archived", "superseded"}:
        return "archive"
    if "PATROL" in name:
        return "patrol"
    if path.name.lower() == "readme.md" or suffix in {".spec.ts", ".spec.tsx"}:
        return "archive"
    if any(fragment in name for fragment in ARCHIVE_FILENAME_MARKERS):
        return "archive"
    if metadata.get("id"):
        return "active"
    return "unknown"

def canonical_in_progress_matches(workspace, legacy_path):
    canonical_dir = workspace / "tasks" / "in_progress"
    if not canonical_dir.exists():
        return []
    legacy_task_id, legacy_name = task_identity(legacy_path)
    matches = []
    for candidate in sorted(canonical_dir.iterdir()):
        if not candidate.is_file() or candidate.name.startswith("."):
            continue
        candidate_task_id, candidate_name = task_identity(candidate)
        if candidate_name == legacy_name or candidate_task_id == legacy_task_id:
            matches.append(candidate)
    return matches

def review_destination_dir(workspace, category):
    target = REVIEW_CATEGORY_DIRECTORIES.get(category)
    return workspace / "tasks" / "review" / target if target else None

def review_destination_matches(workspace, flat_path, category):
    destination_dir = review_destination_dir(workspace, category)
    if destination_dir is None or not destination_dir.exists():
        return []
    flat_task_id, flat_name = task_identity(flat_path)
    matches = []
    for candidate in sorted(destination_dir.iterdir()):
        if not candidate.is_file() or candidate.name.startswith("."):
            continue
        candidate_task_id, candidate_name = task_identity(candidate)
        if candidate_name == flat_name or candidate_task_id == flat_task_id:
            matches.append(candidate)
    return matches
"""


def _remote_doctor_report(target: MaintenanceTarget) -> dict[str, Any]:
    script = (
        _remote_frontmatter_script_body()
        + """
import json
workspace = Path(__WORKSPACE__).expanduser()
tasks_root = workspace / "tasks"
legacy_dir = tasks_root / "in-progress"
review_root = tasks_root / "review"
legacy_entries = []
if legacy_dir.exists():
    for path in sorted(legacy_dir.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.name.lower() == "readme.md":
            continue
        matches = canonical_in_progress_matches(workspace, path)
        if not matches:
            state = "legacy_only"
        elif len(matches) == 1:
            state = compare_duplicate_state(path, matches[0])
        else:
            state = "ambiguous_duplicate"
        legacy_entries.append({
            "task_id": task_identity(path)[0],
            "path": str(path),
            "state": state,
            "canonical_matches": [str(item) for item in matches],
        })
review_entries = []
if review_root.exists():
    for path in sorted(review_root.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        review_entries.append({"path": str(path), "category": classify_review_artifact(path)})
counts = {name: sum(1 for item in review_entries if item["category"] == name) for name in %r}
issues = []
if legacy_entries:
    issues.append(f"legacy_in_progress={len(legacy_entries)}")
flat_review_non_active = counts["decision"] + counts["patrol"] + counts["archive"] + counts["unknown"]
if flat_review_non_active:
    issues.append(f"flat_review_artifacts={flat_review_non_active}")
print(json.dumps({
    "workspace": str(workspace),
    "legacy_in_progress": legacy_entries,
    "review_root": str(review_root),
    "flat_review_entries": review_entries,
    "flat_review_counts": counts,
    "issues": issues,
    "healthy": not issues,
}, ensure_ascii=False))
"""
        % (REVIEW_CATEGORY_ORDER,)
    ).replace("__WORKSPACE__", repr(target.workspace))
    result = _run_ssh(target, f"python3 - <<'PY'\n{script}\nPY")
    if result.returncode != 0:
        raise MeridianWorkflowError(result.stderr.strip() or result.stdout.strip() or "Remote doctor failed")
    return json.loads(result.stdout.strip() or "{}")


def _remote_migrate_in_progress(target: MaintenanceTarget, *, apply: bool) -> dict[str, Any]:
    script = (
        _remote_frontmatter_script_body()
        + """
import json
workspace = Path(__WORKSPACE__).expanduser()
legacy_dir = workspace / "tasks" / "in-progress"
canonical_dir = workspace / "tasks" / "in_progress"
items = []
if legacy_dir.exists():
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for legacy_path in sorted(legacy_dir.iterdir()):
        if not legacy_path.is_file() or legacy_path.name.startswith(".") or legacy_path.name.lower() == "readme.md":
            continue
        task_id, filename = task_identity(legacy_path)
        matches = canonical_in_progress_matches(workspace, legacy_path)
        destination = canonical_dir / filename
        if not matches:
            status = "would_move"
            if __APPLY__:
                legacy_path.replace(destination)
                status = "moved"
            items.append({"task_id": task_id, "status": status, "from": str(legacy_path), "to": str(destination)})
            continue
        if len(matches) > 1:
            items.append({"task_id": task_id, "status": "blocked_ambiguous", "from": str(legacy_path), "canonical_matches": [str(item) for item in matches]})
            continue
        match = matches[0]
        duplicate_state = compare_duplicate_state(legacy_path, match)
        if duplicate_state == "identical_duplicate":
            status = "would_remove_duplicate"
            if __APPLY__:
                legacy_path.unlink()
                status = "removed_duplicate"
            items.append({"task_id": task_id, "status": status, "from": str(legacy_path), "to": str(match)})
            continue
        items.append({"task_id": task_id, "status": "blocked_divergent", "from": str(legacy_path), "to": str(match)})
    if __APPLY__:
        legacy_dir.mkdir(parents=True, exist_ok=True)
        readme = legacy_dir / "README.md"
        if not readme.exists():
            readme.write_text("# Deprecated queue\\n\\nThis directory is deprecated. Use `tasks/in_progress/` instead.\\n", encoding="utf-8")
summary = {
    "moved": sum(1 for item in items if item["status"] == "moved"),
    "removed_duplicates": sum(1 for item in items if item["status"] == "removed_duplicate"),
    "blocked": sum(1 for item in items if item["status"].startswith("blocked")),
    "unchanged": sum(1 for item in items if item["status"].startswith("would_")),
}
print(json.dumps({"workspace": str(workspace), "apply": __APPLY__, "items": items, "summary": summary}, ensure_ascii=False))
"""
    ).replace("__WORKSPACE__", repr(target.workspace)).replace("__APPLY__", "True" if apply else "False")
    result = _run_ssh(target, f"python3 - <<'PY'\n{script}\nPY")
    if result.returncode != 0:
        raise MeridianWorkflowError(result.stderr.strip() or result.stdout.strip() or "Remote in-progress migration failed")
    return json.loads(result.stdout.strip() or "{}")


def _remote_migrate_review(target: MaintenanceTarget, *, apply: bool) -> dict[str, Any]:
    script = (
        _remote_frontmatter_script_body()
        + """
import json
workspace = Path(__WORKSPACE__).expanduser()
review_root = workspace / "tasks" / "review"
items = []
if review_root.exists():
    for flat_path in sorted(review_root.iterdir()):
        if not flat_path.is_file() or flat_path.name.startswith("."):
            continue
        category = classify_review_artifact(flat_path)
        destination_dir = review_destination_dir(workspace, category)
        task_id, filename = task_identity(flat_path)
        if destination_dir is None:
            items.append({"task_id": task_id, "category": category, "status": "blocked_unknown_category", "from": str(flat_path)})
            continue
        destination_dir.mkdir(parents=True, exist_ok=True)
        matches = review_destination_matches(workspace, flat_path, category)
        destination = destination_dir / filename
        if not matches:
            status = "would_move"
            if __APPLY__:
                flat_path.replace(destination)
                status = "moved"
            items.append({"task_id": task_id, "category": category, "status": status, "from": str(flat_path), "to": str(destination)})
            continue
        if len(matches) > 1:
            items.append({"task_id": task_id, "category": category, "status": "blocked_ambiguous", "from": str(flat_path), "matches": [str(item) for item in matches]})
            continue
        match = matches[0]
        if compare_duplicate_state(flat_path, match) == "identical_duplicate":
            status = "would_remove_duplicate"
            if __APPLY__:
                flat_path.unlink()
                status = "removed_duplicate"
            items.append({"task_id": task_id, "category": category, "status": status, "from": str(flat_path), "to": str(match)})
            continue
        items.append({"task_id": task_id, "category": category, "status": "blocked_divergent", "from": str(flat_path), "to": str(match)})
summary = {
    "moved": sum(1 for item in items if item["status"] == "moved"),
    "removed_duplicates": sum(1 for item in items if item["status"] == "removed_duplicate"),
    "blocked": sum(1 for item in items if item["status"].startswith("blocked")),
    "unknown": sum(1 for item in items if item["status"] == "blocked_unknown_category"),
}
print(json.dumps({"workspace": str(workspace), "apply": __APPLY__, "items": items, "summary": summary}, ensure_ascii=False))
"""
    ).replace("__WORKSPACE__", repr(target.workspace)).replace("__APPLY__", "True" if apply else "False")
    result = _run_ssh(target, f"python3 - <<'PY'\n{script}\nPY")
    if result.returncode != 0:
        raise MeridianWorkflowError(result.stderr.strip() or result.stdout.strip() or "Remote review migration failed")
    return json.loads(result.stdout.strip() or "{}")


def _canonical_in_progress_matches(workspace: Path, legacy_path: Path) -> list[Path]:
    canonical_dir = canonical_queue_dir(workspace, "in_progress")
    if not canonical_dir.exists():
        return []
    legacy_task_id, legacy_name = _task_identity(legacy_path)
    matches: list[Path] = []
    for candidate in sorted(canonical_dir.iterdir()):
        if not candidate.is_file() or candidate.name.startswith("."):
            continue
        candidate_task_id, candidate_name = _task_identity(candidate)
        if candidate_name == legacy_name or candidate_task_id == legacy_task_id:
            matches.append(candidate)
    return matches


def _compare_duplicate_state(legacy_path: Path, canonical_path: Path) -> str:
    return "identical_duplicate" if _read_text(legacy_path) == _read_text(canonical_path) else "divergent_duplicate"


def classify_review_artifact(path: Path) -> str:
    metadata = _task_metadata(path)
    name = path.name.upper()
    status = str(metadata.get("status") or "").strip().lower()
    review_kind = str(metadata.get("review_kind") or "").strip().lower()
    suffix = "".join(path.suffixes).lower()
    if is_review_decision_artifact(path, metadata):
        return "decision"
    if any(fragment in name for fragment in DECISION_FILENAME_MARKERS):
        return "decision"
    if review_kind == "patrol" or "PATROL" in name:
        return "patrol"
    if status in {"archived", "superseded"}:
        return "archive"
    if path.name.lower() == "readme.md" or suffix in {".spec.ts", ".spec.tsx"}:
        return "archive"
    if any(fragment in name for fragment in ARCHIVE_FILENAME_MARKERS):
        return "archive"
    if metadata.get("id"):
        return "active"
    return "unknown"


def _review_destination_dir(workspace: Path, category: str) -> Path | None:
    target = REVIEW_CATEGORY_DIRECTORIES.get(category)
    if not target:
        return None
    return workspace / "tasks" / "review" / target


def _review_destination_matches(workspace: Path, flat_path: Path, category: str) -> list[Path]:
    destination_dir = _review_destination_dir(workspace, category)
    if destination_dir is None or not destination_dir.exists():
        return []
    flat_task_id, flat_name = _task_identity(flat_path)
    matches: list[Path] = []
    for candidate in sorted(destination_dir.iterdir()):
        if not candidate.is_file() or candidate.name.startswith("."):
            continue
        candidate_task_id, candidate_name = _task_identity(candidate)
        if candidate_name == flat_name or candidate_task_id == flat_task_id:
            matches.append(candidate)
    return matches


def meridian_doctor_report(workspace: str | Path) -> dict[str, Any]:
    workspace_path = Path(workspace).resolve()
    tasks_root = workspace_path / "tasks"
    legacy_dir = tasks_root / "in-progress"
    review_root = tasks_root / "review"

    legacy_entries: list[dict[str, Any]] = []
    if legacy_dir.exists():
        for path in sorted(legacy_dir.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.name.lower() == "readme.md":
                continue
            matches = _canonical_in_progress_matches(workspace_path, path)
            if not matches:
                state = "legacy_only"
            elif len(matches) == 1:
                state = _compare_duplicate_state(path, matches[0])
            else:
                state = "ambiguous_duplicate"
            legacy_entries.append(
                {
                    "task_id": _task_identity(path)[0],
                    "path": str(path),
                    "state": state,
                    "canonical_matches": [str(item) for item in matches],
                }
            )

    review_entries: list[dict[str, Any]] = []
    if review_root.exists():
        for path in sorted(review_root.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            review_entries.append(
                {
                    "path": str(path),
                    "category": classify_review_artifact(path),
                }
            )

    review_counts = {
        category: sum(1 for item in review_entries if item["category"] == category)
        for category in REVIEW_CATEGORY_ORDER
    }
    issues: list[str] = []
    if legacy_entries:
        issues.append(f"legacy_in_progress={len(legacy_entries)}")
    flat_review_non_active = review_counts["decision"] + review_counts["patrol"] + review_counts["archive"] + review_counts["unknown"]
    if flat_review_non_active:
        issues.append(f"flat_review_artifacts={flat_review_non_active}")
    return {
        "workspace": str(workspace_path),
        "legacy_in_progress": legacy_entries,
        "review_root": str(review_root),
        "flat_review_entries": review_entries,
        "flat_review_counts": review_counts,
        "issues": issues,
        "healthy": not issues,
    }


def run_meridian_doctor(workspace: str | Path) -> dict[str, Any]:
    target = _resolve_maintenance_target(workspace)
    if target.mode == "ssh":
        return _remote_doctor_report(target)
    return meridian_doctor_report(target.workspace)


def format_meridian_doctor_report(report: dict[str, Any]) -> str:
    lines = ["Meridian doctor", f"  Workspace:      {report.get('workspace') or '-'}"]
    issues = report.get("issues") or []
    lines.append(f"  Healthy:        {'yes' if report.get('healthy') else 'no'}")
    if issues:
        lines.append(f"  Issues:         {', '.join(issues)}")

    legacy_entries = report.get("legacy_in_progress") or []
    if legacy_entries:
        lines.append("  Legacy in-progress:")
        for entry in legacy_entries:
            lines.append(
                "    "
                f"{entry.get('task_id') or '-'} "
                f"state={entry.get('state') or '-'} "
                f"path={entry.get('path') or '-'}"
            )

    review_counts = report.get("flat_review_counts") or {}
    if any(review_counts.values()):
        lines.append("  Flat review artifacts:")
        for category in REVIEW_CATEGORY_ORDER:
            count = int(review_counts.get(category) or 0)
            if count:
                lines.append(f"    {category}={count}")
    if report.get("healthy"):
        lines.append("  No Meridian maintenance issues detected.")
    return "\n".join(lines)


def _ensure_legacy_readme(legacy_dir: Path) -> None:
    legacy_dir.mkdir(parents=True, exist_ok=True)
    readme = legacy_dir / "README.md"
    if readme.exists():
        return
    readme.write_text(
        "# Deprecated queue\n\n"
        "This directory is deprecated. Use `tasks/in_progress/` instead.\n",
        encoding="utf-8",
    )


def migrate_in_progress_queue(workspace: str | Path, *, apply: bool = False) -> dict[str, Any]:
    workspace_path = Path(workspace).resolve()
    legacy_dir = workspace_path / "tasks" / "in-progress"
    canonical_dir = canonical_queue_dir(workspace_path, "in_progress")
    items: list[dict[str, Any]] = []

    if not legacy_dir.exists():
        return {
            "workspace": str(workspace_path),
            "apply": apply,
            "items": items,
            "summary": {"moved": 0, "removed_duplicates": 0, "blocked": 0, "unchanged": 0},
        }

    canonical_dir.mkdir(parents=True, exist_ok=True)
    for legacy_path in sorted(legacy_dir.iterdir()):
        if not legacy_path.is_file() or legacy_path.name.startswith(".") or legacy_path.name.lower() == "readme.md":
            continue
        task_id, filename = _task_identity(legacy_path)
        canonical_matches = _canonical_in_progress_matches(workspace_path, legacy_path)
        destination = canonical_dir / filename
        if not canonical_matches:
            status = "would_move"
            if apply:
                legacy_path.replace(destination)
                status = "moved"
            items.append({"task_id": task_id, "status": status, "from": str(legacy_path), "to": str(destination)})
            continue

        if len(canonical_matches) > 1:
            items.append(
                {
                    "task_id": task_id,
                    "status": "blocked_ambiguous",
                    "from": str(legacy_path),
                    "canonical_matches": [str(item) for item in canonical_matches],
                }
            )
            continue

        canonical_match = canonical_matches[0]
        duplicate_state = _compare_duplicate_state(legacy_path, canonical_match)
        if duplicate_state == "identical_duplicate":
            status = "would_remove_duplicate"
            if apply:
                legacy_path.unlink()
                status = "removed_duplicate"
            items.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "from": str(legacy_path),
                    "to": str(canonical_match),
                }
            )
            continue

        items.append(
            {
                "task_id": task_id,
                "status": "blocked_divergent",
                "from": str(legacy_path),
                "to": str(canonical_match),
            }
        )

    if apply:
        _ensure_legacy_readme(legacy_dir)

    summary = {
        "moved": sum(1 for item in items if item["status"] == "moved"),
        "removed_duplicates": sum(1 for item in items if item["status"] == "removed_duplicate"),
        "blocked": sum(1 for item in items if item["status"].startswith("blocked")),
        "unchanged": sum(1 for item in items if item["status"].startswith("would_")),
    }
    return {
        "workspace": str(workspace_path),
        "apply": apply,
        "items": items,
        "summary": summary,
    }


def run_migrate_in_progress_queue(workspace: str | Path, *, apply: bool = False) -> dict[str, Any]:
    target = _resolve_maintenance_target(workspace)
    if target.mode == "ssh":
        return _remote_migrate_in_progress(target, apply=apply)
    return migrate_in_progress_queue(target.workspace, apply=apply)


def format_in_progress_migration(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "Meridian in-progress migration",
        f"  Workspace:      {report.get('workspace') or '-'}",
        f"  Mode:           {'apply' if report.get('apply') else 'dry-run'}",
        "  Summary:        "
        f"moved={summary.get('moved', 0)} "
        f"removed_duplicates={summary.get('removed_duplicates', 0)} "
        f"blocked={summary.get('blocked', 0)} "
        f"pending={summary.get('unchanged', 0)}",
    ]
    for item in report.get("items") or []:
        lines.append(
            "  "
            f"{item.get('task_id') or '-'} "
            f"status={item.get('status') or '-'} "
            f"from={item.get('from') or '-'}"
        )
    if not report.get("items"):
        lines.append("  No legacy in-progress tasks found.")
    return "\n".join(lines)


def migrate_review_queue(workspace: str | Path, *, apply: bool = False) -> dict[str, Any]:
    workspace_path = Path(workspace).resolve()
    review_root = workspace_path / "tasks" / "review"
    items: list[dict[str, Any]] = []

    if not review_root.exists():
        return {
            "workspace": str(workspace_path),
            "apply": apply,
            "items": items,
            "summary": {"moved": 0, "removed_duplicates": 0, "blocked": 0, "unknown": 0},
        }

    for flat_path in sorted(review_root.iterdir()):
        if not flat_path.is_file() or flat_path.name.startswith("."):
            continue
        category = classify_review_artifact(flat_path)
        destination_dir = _review_destination_dir(workspace_path, category)
        task_id, filename = _task_identity(flat_path)

        if destination_dir is None:
            items.append(
                {
                    "task_id": task_id,
                    "category": category,
                    "status": "blocked_unknown_category",
                    "from": str(flat_path),
                }
            )
            continue

        destination_dir.mkdir(parents=True, exist_ok=True)
        matches = _review_destination_matches(workspace_path, flat_path, category)
        destination = destination_dir / filename
        if not matches:
            status = "would_move"
            if apply:
                flat_path.replace(destination)
                status = "moved"
            items.append(
                {
                    "task_id": task_id,
                    "category": category,
                    "status": status,
                    "from": str(flat_path),
                    "to": str(destination),
                }
            )
            continue

        if len(matches) > 1:
            items.append(
                {
                    "task_id": task_id,
                    "category": category,
                    "status": "blocked_ambiguous",
                    "from": str(flat_path),
                    "matches": [str(item) for item in matches],
                }
            )
            continue

        match = matches[0]
        if _compare_duplicate_state(flat_path, match) == "identical_duplicate":
            status = "would_remove_duplicate"
            if apply:
                flat_path.unlink()
                status = "removed_duplicate"
            items.append(
                {
                    "task_id": task_id,
                    "category": category,
                    "status": status,
                    "from": str(flat_path),
                    "to": str(match),
                }
            )
            continue

        items.append(
            {
                "task_id": task_id,
                "category": category,
                "status": "blocked_divergent",
                "from": str(flat_path),
                "to": str(match),
            }
        )

    summary = {
        "moved": sum(1 for item in items if item["status"] == "moved"),
        "removed_duplicates": sum(1 for item in items if item["status"] == "removed_duplicate"),
        "blocked": sum(1 for item in items if item["status"].startswith("blocked")),
        "unknown": sum(1 for item in items if item["status"] == "blocked_unknown_category"),
    }
    return {
        "workspace": str(workspace_path),
        "apply": apply,
        "items": items,
        "summary": summary,
    }


def run_migrate_review_queue(workspace: str | Path, *, apply: bool = False) -> dict[str, Any]:
    target = _resolve_maintenance_target(workspace)
    if target.mode == "ssh":
        return _remote_migrate_review(target, apply=apply)
    return migrate_review_queue(target.workspace, apply=apply)


def format_review_migration(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "Meridian review migration",
        f"  Workspace:      {report.get('workspace') or '-'}",
        f"  Mode:           {'apply' if report.get('apply') else 'dry-run'}",
        "  Summary:        "
        f"moved={summary.get('moved', 0)} "
        f"removed_duplicates={summary.get('removed_duplicates', 0)} "
        f"blocked={summary.get('blocked', 0)} "
        f"unknown={summary.get('unknown', 0)}",
    ]
    for item in report.get("items") or []:
        lines.append(
            "  "
            f"{item.get('task_id') or '-'} "
            f"category={item.get('category') or '-'} "
            f"status={item.get('status') or '-'} "
            f"from={item.get('from') or '-'}"
        )
    if not report.get("items"):
        lines.append("  No flat review artifacts found.")
    return "\n".join(lines)


def workspace_from_meridian_path(workspace: str | Path | None) -> Path:
    if workspace is None:
        return Path(".").resolve()
    path = Path(workspace).resolve()
    if path.name == "tasks":
        return path.parent
    if path.name == "in-progress":
        return workspace_root_from_task_path(path)
    if (path / "tasks").exists():
        return path
    raise MeridianWorkflowError(f"Workspace does not look like a Meridian checkout: {path}")
