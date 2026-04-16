"""Meridian review-signal quality gate orchestration.

This module consumes Meridian workflow events and runs non-blocking quality and
security checks when tasks enter ``review``. Results are persisted in a
shared Meridian state directory so Matthew, Fatih, the default gateway profile,
and CLI status commands all consume the same evidence.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_utils import atomic_json_write

from hermes_cli.meridian_runtime import EVENT_LOG_PATH, emit_meridian_event, parse_iso_datetime
from hermes_cli.meridian_review import is_review_decision_artifact


DEFAULT_REMOTE_WORKSPACE = str(Path.home() / "Meridian")
DEFAULT_EVENT_TYPES = frozenset({"task_transitioned"})
DEFAULT_REVIEW_QUEUE = "review"
SEVERITY_ORDER = {"blocking": 0, "review": 1, "debt": 2, "advisory": 3, "passed": 4, "skipped": 5}
SECTION_HEADER_RE = re.compile(r"^-- (?P<section>.+?) --$")
PIP_AUDIT_ROW_RE = re.compile(r"^(?P<name>\S+)\s+(?P<version>\S+)\s+(?P<id>\S+)\s+(?P<fix>.+)$")
BANDIT_SEVERITY_RE = re.compile(r"Severity:\s+(Low|Medium|High)", re.IGNORECASE)
SEMGREP_FINDING_RE = re.compile(r"^\s+❯❱\s+(?P<rule>[\w\.\-]+)")

BLOCKING_SEMGREP_RULE_FRAGMENTS = (
    "sql-injection",
    "command-injection",
    "insecure-deserialization",
    "path-traversal",
    "unvalidated-password",
    "auth-bypass",
    "jwt-none-alg",
)
REVIEW_SEMGREP_RULE_FRAGMENTS = (
    "insecure-hash-algorithms-md5",
    "request-with-http",
    "var-in-href",
    "dangerous-subprocess-use",
)
CRITICAL_DEPENDENCY_PACKAGES = frozenset(
    {
        "django",
        "djangorestframework",
        "djangorestframework-simplejwt",
        "cryptography",
        "pillow",
    }
)

LANE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "name": "backend-quality",
        "kind": "quality",
        "path": "scripts/backend-quality-check.sh",
        "cwd": ".",
        "command": "bash scripts/backend-quality-check.sh",
    },
    {
        "name": "backend-security",
        "kind": "security",
        "path": "scripts/backend-security-check.sh",
        "cwd": ".",
        "command": "bash scripts/backend-security-check.sh",
    },
    {
        "name": "frontend-lint",
        "kind": "quality",
        "path": "frontend/package.json",
        "cwd": "frontend",
        "command": "npm run lint",
    },
    {
        "name": "frontend-build",
        "kind": "quality",
        "path": "frontend/package.json",
        "cwd": "frontend",
        "command": "npm run build",
    },
    {
        "name": "frontend-security",
        "kind": "security",
        "path": "frontend/package.json",
        "cwd": "frontend",
        "command": "npm run security:check",
    },
)


@dataclass(frozen=True)
class Executor:
    mode: str
    workspace: str
    host: str | None = None
    user: str | None = None
    key: str | None = None
    password: str | None = None


def shared_meridian_dir() -> Path:
    override = (os.getenv("HERMES_MERIDIAN_SHARED_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".hermes" / "meridian"


STATE_PATH = shared_meridian_dir() / "quality_gate_state.json"
REPORTS_DIR = shared_meridian_dir() / "review_signals"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    state = _read_json(path)
    if not isinstance(state.get("results"), dict):
        state["results"] = {}
    if not isinstance(state.get("processed_event_ids"), list):
        state["processed_event_ids"] = []
    if not isinstance(state.get("line_cursor"), int):
        state["line_cursor"] = 0
    return state


def _save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, state)


def _normalize_workspace(workspace: str | Path | None) -> str:
    if workspace is None:
        env_workspace = (os.getenv("HERMES_MERIDIAN_WORKSPACE") or "").strip()
        return env_workspace or DEFAULT_REMOTE_WORKSPACE
    return str(Path(workspace).expanduser()) if str(workspace).strip() else DEFAULT_REMOTE_WORKSPACE


def _terminal_ssh_settings() -> dict[str, str] | None:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return None
    terminal = config.get("terminal") or {}
    if str(terminal.get("backend") or "").strip().lower() != "ssh":
        return None
    host = str(os.getenv("TERMINAL_SSH_HOST") or terminal.get("ssh_host") or "").strip()
    user = str(os.getenv("TERMINAL_SSH_USER") or terminal.get("ssh_user") or "").strip()
    key = str(os.getenv("TERMINAL_SSH_KEY") or terminal.get("ssh_key") or "").strip()
    password = str(os.getenv("TERMINAL_SSH_PASSWORD") or "").strip()
    cwd = str(os.getenv("HERMES_MERIDIAN_WORKSPACE") or terminal.get("cwd") or "").strip()
    if not host or not user:
        return None
    return {
        "host": host,
        "user": user,
        "key": key,
        "password": password,
        "workspace": cwd or DEFAULT_REMOTE_WORKSPACE,
    }


def _quality_ssh_settings() -> dict[str, str] | None:
    host = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_HOST")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_HOST")
        or ""
    ).strip()
    user = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_USER")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_USER")
        or ""
    ).strip()
    key = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_KEY")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_KEY")
        or ""
    ).strip()
    password = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_SSH_PASSWORD")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_SSH_PASSWORD")
        or ""
    ).strip()
    workspace = str(
        os.getenv("HERMES_MERIDIAN_QUALITY_WORKSPACE")
        or os.getenv("HERMES_MERIDIAN_NOTIFY_WORKSPACE")
        or os.getenv("HERMES_MERIDIAN_WORKSPACE")
        or ""
    ).strip()
    if host and user:
        return {
            "host": host,
            "user": user,
            "key": key,
            "password": password,
            "workspace": workspace or DEFAULT_REMOTE_WORKSPACE,
        }
    return _terminal_ssh_settings()


def _build_executor(workspace: str | Path | None = None) -> Executor:
    normalized_workspace = _normalize_workspace(workspace)
    candidate = Path(normalized_workspace).expanduser()
    if candidate.exists():
        return Executor(mode="local", workspace=str(candidate.resolve()))
    ssh = _quality_ssh_settings()
    if ssh:
        return Executor(
            mode="ssh",
            workspace=ssh.get("workspace") or normalized_workspace,
            host=ssh.get("host"),
            user=ssh.get("user"),
            key=ssh.get("key"),
            password=ssh.get("password"),
        )
    return Executor(mode="local", workspace=str(candidate))


def _run_local(command: str, *, cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _run_ssh(executor: Executor, command: str, *, cwd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    remote_command = f"cd {shlex.quote(cwd)} && {command}"
    if executor.password:
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
            f"{executor.user}@{executor.host}",
            remote_command,
        ]
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
        if executor.key:
            cmd.extend(["-i", executor.key])
        cmd.extend([f"{executor.user}@{executor.host}", remote_command])
    env = os.environ.copy()
    if executor.password:
        env["SSHPASS"] = executor.password
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _run_command(executor: Executor, command: str, *, cwd: str, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    if executor.mode == "ssh":
        return _run_ssh(executor, command, cwd=cwd, timeout=timeout)
    return _run_local(command, cwd=cwd, timeout=timeout)


def _available_lanes(executor: Executor) -> list[dict[str, str]]:
    available: list[dict[str, str]] = []
    for lane in LANE_DEFINITIONS:
        if executor.mode == "local":
            probe = Path(executor.workspace) / lane["path"]
            if probe.exists():
                available.append(dict(lane))
        else:
            probe_cmd = f"test -e {shlex.quote(str(Path(executor.workspace) / lane['path']))}"
            result = _run_command(executor, probe_cmd, cwd=executor.workspace, timeout=30)
            if result.returncode == 0:
                available.append(dict(lane))
    return available


def _normalize_path_list(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    normalized: list[str] = []
    for item in items:
        text = str(item).strip().replace("\\", "/").lstrip("./")
        if not text:
            continue
        normalized.append(text)
    return normalized


def _task_scope_from_paths(scope_entries: list[str], *, reason: str, empty_reason: str) -> dict[str, Any]:
    if not scope_entries:
        return {"mode": "full", "reason": empty_reason, "paths": []}

    backend_hit = False
    frontend_hit = False
    non_code_hit = False
    for entry in scope_entries:
        if entry.startswith("backend/"):
            backend_hit = True
            continue
        if entry.startswith("frontend/"):
            frontend_hit = True
            continue
        non_code_hit = True

    if backend_hit or frontend_hit:
        categories: list[str] = []
        if backend_hit:
            categories.append("backend")
        if frontend_hit:
            categories.append("frontend")
        return {
            "mode": "scoped",
            "reason": reason,
            "paths": scope_entries,
            "categories": categories,
        }

    if non_code_hit:
        return {
            "mode": "docs_only",
            "reason": f"{reason}_non_code_only",
            "paths": scope_entries,
            "categories": [],
        }

    return {"mode": "full", "reason": "unclassified_scope", "paths": scope_entries}


def _task_scope_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    linked_files = _normalize_path_list(metadata.get("linked_files"))
    linked_dirs = _normalize_path_list(metadata.get("linked_dirs"))
    return _task_scope_from_paths(
        linked_files + linked_dirs,
        reason="linked_paths",
        empty_reason="no_scope_metadata",
    )


def _filter_lanes_for_scope(available_lanes: list[dict[str, str]], scope: dict[str, Any]) -> list[dict[str, str]]:
    mode = scope.get("mode")
    if mode == "full":
        return [dict(lane) for lane in available_lanes]
    if mode == "docs_only":
        return []
    categories = set(scope.get("categories") or [])
    selected: list[dict[str, str]] = []
    for lane in available_lanes:
        name = str(lane.get("name") or "")
        if "backend" in categories and name.startswith("backend-"):
            selected.append(dict(lane))
            continue
        if "frontend" in categories and name.startswith("frontend-"):
            selected.append(dict(lane))
    return selected


def _trim_output(text: str, *, max_lines: int = 80, max_chars: int = 5000) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    lines = normalized.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(normalized.splitlines()) - max_lines} more lines)"]
    clipped = "\n".join(lines)
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 18].rstrip() + "\n... (truncated)"
    return clipped


def _locate_task_metadata_local(executor: Executor, task_id: str) -> dict[str, Any]:
    tasks_root = Path(executor.workspace) / "tasks"
    if not tasks_root.exists():
        return {}
    for path in tasks_root.rglob("*.md"):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            metadata = _parse_task_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        candidate_id = str(metadata.get("id") or path.stem)
        if candidate_id == task_id or path.name == task_id:
            return metadata
    return {}


def _locate_task_metadata_ssh(executor: Executor, task_id: str) -> dict[str, Any]:
    script = """
import json
from pathlib import Path
import yaml

workspace = Path(__WORKSPACE__).expanduser()
task_id = __TASK_ID__
tasks_root = workspace / "tasks"
result = {}
if tasks_root.exists():
    for path in sorted(tasks_root.rglob("*.md")):
        if not path.is_file() or path.name.startswith('.'):
            continue
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            continue
        metadata = {}
        if content.startswith('---\\n'):
            closing = content.find('\\n---\\n', 4)
            if closing != -1:
                loaded = yaml.safe_load(content[4:closing]) or {}
                if isinstance(loaded, dict):
                    metadata = loaded
        candidate_id = str(metadata.get('id') or path.stem)
        if candidate_id == task_id or path.name == task_id:
            result = metadata
            break
print(json.dumps(result, ensure_ascii=False))
""".replace("__WORKSPACE__", repr(executor.workspace)).replace("__TASK_ID__", repr(task_id))
    result = _run_command(
        executor,
        f"python3 - <<'PY'\n{script}\nPY",
        cwd=executor.workspace,
        timeout=45,
    )
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _task_metadata_for_scope(executor: Executor, task_id: str) -> dict[str, Any]:
    if executor.mode == "ssh":
        return _locate_task_metadata_ssh(executor, task_id)
    return _locate_task_metadata_local(executor, task_id)


def _git_diff_candidates(metadata: dict[str, Any]) -> list[tuple[str, str]]:
    branch = str(metadata.get("pr_branch") or metadata.get("branch") or metadata.get("source_branch") or "").strip()
    base_branch = str(metadata.get("base_branch") or metadata.get("target_branch") or "main").strip() or "main"
    candidates: list[tuple[str, str]] = []
    if branch:
        candidates.extend(
            [
                (f"origin/{base_branch}...{branch}", "git_branch_diff"),
                (f"{base_branch}...{branch}", "git_branch_diff"),
            ]
        )
    candidates.extend(
        [
            ("HEAD~1..HEAD", "git_head_diff"),
            ("HEAD^..HEAD", "git_head_diff"),
        ]
    )
    return candidates


def _git_changed_paths(executor: Executor, metadata: dict[str, Any]) -> tuple[list[str], str]:
    for revision_range, reason in _git_diff_candidates(metadata):
        command = f"git diff --name-only --relative {shlex.quote(revision_range)}"
        result = _run_command(executor, command, cwd=executor.workspace, timeout=45)
        if result.returncode != 0:
            continue
        paths = _normalize_path_list(result.stdout.splitlines())
        if paths:
            return paths, reason
    return [], ""


def _task_scope(executor: Executor, metadata: dict[str, Any]) -> dict[str, Any]:
    metadata_scope = _task_scope_from_metadata(metadata)
    if metadata_scope.get("mode") != "full":
        return metadata_scope

    diff_paths, reason = _git_changed_paths(executor, metadata)
    if diff_paths:
        return _task_scope_from_paths(
            diff_paths,
            reason=reason or "git_diff",
            empty_reason="no_git_diff_scope",
        )
    return metadata_scope


def _summarize_lanes(lanes: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"passed": 0, "failed": 0, "skipped": 0, "quality_failures": 0, "security_failures": 0}
    for lane in lanes:
        status = lane.get("status")
        if status == "passed":
            summary["passed"] += 1
        elif status == "failed":
            summary["failed"] += 1
            if lane.get("kind") == "security":
                summary["security_failures"] += 1
            else:
                summary["quality_failures"] += 1
        else:
            summary["skipped"] += 1
    return summary


def _extract_sections(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = "general"
    sections[current] = []
    for line in (output or "").splitlines():
        match = SECTION_HEADER_RE.match(line.strip())
        if match:
            current = match.group("section").strip().lower()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _make_finding(*, bucket: str, summary: str, evidence: str, source: str) -> dict[str, str]:
    return {
        "bucket": bucket,
        "summary": summary,
        "evidence": evidence,
        "source": source,
    }


def _classify_backend_quality(lane: dict[str, Any]) -> list[dict[str, str]]:
    if lane.get("status") == "passed":
        return [_make_finding(bucket="passed", summary="Backend quality checks passed", evidence="", source=lane["name"])]
    findings: list[dict[str, str]] = []
    sections = _extract_sections(lane.get("output") or "")
    pytest_output = sections.get("pytest", "")
    if pytest_output:
        findings.append(
            _make_finding(
                bucket="blocking",
                summary="Pytest failed in backend quality gate",
                evidence=_trim_output(pytest_output, max_lines=16, max_chars=1200),
                source="backend-quality:pytest",
            )
        )
    ruff_bits = []
    for key in ("ruff format --check", "ruff lint"):
        if sections.get(key):
            ruff_bits.append(_trim_output(sections[key], max_lines=12, max_chars=900))
    if ruff_bits:
        findings.append(
            _make_finding(
                bucket="review",
                summary="Ruff reported backend style or lint issues",
                evidence="\n\n".join(bit for bit in ruff_bits if bit),
                source="backend-quality:ruff",
            )
        )
    if not findings:
        findings.append(
            _make_finding(
                bucket="review",
                summary="Backend quality gate failed",
                evidence=_trim_output(lane.get("output") or "", max_lines=20, max_chars=1200),
                source=lane["name"],
            )
        )
    return findings


def _classify_pip_audit(section_output: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    rows: list[tuple[str, str, str, str]] = []
    for raw in section_output.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("Found ") or stripped.startswith("Name") or stripped.startswith("-"):
            continue
        match = PIP_AUDIT_ROW_RE.match(stripped)
        if not match:
            continue
        rows.append((match.group("name"), match.group("version"), match.group("id"), match.group("fix").strip()))
    if not rows:
        return findings
    blocking_rows = [row for row in rows if row[0].lower() in CRITICAL_DEPENDENCY_PACKAGES]
    debt_rows = [row for row in rows if row not in blocking_rows]
    if blocking_rows:
        lines = [f"{name} {version} {vuln_id} -> {fix}" for name, version, vuln_id, fix in blocking_rows[:8]]
        findings.append(
            _make_finding(
                bucket="review",
                summary=f"Dependency vulnerabilities found in critical runtime packages ({len(blocking_rows)})",
                evidence="\n".join(lines),
                source="backend-security:pip-audit",
            )
        )
    if debt_rows:
        lines = [f"{name} {version} {vuln_id} -> {fix}" for name, version, vuln_id, fix in debt_rows[:8]]
        findings.append(
            _make_finding(
                bucket="debt",
                summary=f"Dependency vulnerability backlog detected ({len(debt_rows)})",
                evidence="\n".join(lines),
                source="backend-security:pip-audit",
            )
        )
    return findings


def _classify_bandit(section_output: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    interesting_lines: list[str] = []
    for line in section_output.splitlines():
        match = BANDIT_SEVERITY_RE.search(line)
        if match:
            severity = match.group(1).lower()
            severity_counts[severity] += 1
        if "Location:" in line or "Issue:" in line:
            interesting_lines.append(line.strip())
    if severity_counts["high"]:
        findings.append(
            _make_finding(
                bucket="blocking",
                summary=f"Bandit reported {severity_counts['high']} high-severity finding(s)",
                evidence="\n".join(interesting_lines[:10]),
                source="backend-security:bandit",
            )
        )
    if severity_counts["medium"]:
        findings.append(
            _make_finding(
                bucket="review",
                summary=f"Bandit reported {severity_counts['medium']} medium-severity finding(s)",
                evidence="\n".join(interesting_lines[:10]),
                source="backend-security:bandit",
            )
        )
    if severity_counts["low"]:
        findings.append(
            _make_finding(
                bucket="advisory",
                summary=f"Bandit reported {severity_counts['low']} low-severity finding(s)",
                evidence="\n".join(interesting_lines[:10]),
                source="backend-security:bandit",
            )
        )
    return findings


def _semgrep_bucket(rule: str) -> str:
    lowered = rule.lower()
    if any(fragment in lowered for fragment in BLOCKING_SEMGREP_RULE_FRAGMENTS):
        return "blocking"
    if any(fragment in lowered for fragment in REVIEW_SEMGREP_RULE_FRAGMENTS):
        return "review"
    return "review"


def _classify_semgrep(section_output: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    buckets: dict[str, list[str]] = {"blocking": [], "review": [], "advisory": []}
    current_file = ""
    for line in section_output.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("apps/") or stripped.startswith("config/"):
            current_file = stripped.strip()
            continue
        match = SEMGREP_FINDING_RE.match(stripped)
        if not match:
            continue
        rule = match.group("rule")
        bucket = _semgrep_bucket(rule)
        entry = f"{rule} ({current_file or 'unknown file'})"
        buckets[bucket].append(entry)
    for bucket in ("blocking", "review", "advisory"):
        if buckets[bucket]:
            findings.append(
                _make_finding(
                    bucket=bucket,
                    summary=f"Semgrep produced {len(buckets[bucket])} {bucket} finding(s)",
                    evidence="\n".join(buckets[bucket][:10]),
                    source="backend-security:semgrep",
                )
            )
    return findings


def _classify_backend_security(lane: dict[str, Any]) -> list[dict[str, str]]:
    if lane.get("status") == "passed":
        return [_make_finding(bucket="passed", summary="Backend security checks passed", evidence="", source=lane["name"])]
    findings: list[dict[str, str]] = []
    sections = _extract_sections(lane.get("output") or "")
    findings.extend(_classify_pip_audit(sections.get("pip-audit", "")))
    findings.extend(_classify_bandit(sections.get("bandit", "")))
    findings.extend(_classify_semgrep(sections.get("semgrep", "")))
    if not findings:
        findings.append(
            _make_finding(
                bucket="review",
                summary="Backend security gate failed",
                evidence=_trim_output(lane.get("output") or "", max_lines=20, max_chars=1200),
                source=lane["name"],
            )
        )
    return findings


def _classify_frontend_lint(lane: dict[str, Any]) -> list[dict[str, str]]:
    if lane.get("status") == "passed":
        return [_make_finding(bucket="passed", summary="Frontend lint passed", evidence="", source=lane["name"])]
    return [
        _make_finding(
            bucket="review",
            summary="Frontend lint reported issues",
            evidence=_trim_output(lane.get("output") or "", max_lines=16, max_chars=1200),
            source=lane["name"],
        )
    ]


def _classify_frontend_build(lane: dict[str, Any]) -> list[dict[str, str]]:
    if lane.get("status") == "passed":
        return [_make_finding(bucket="passed", summary="Frontend build passed", evidence="", source=lane["name"])]
    return [
        _make_finding(
            bucket="blocking",
            summary="Frontend build failed",
            evidence=_trim_output(lane.get("output") or "", max_lines=16, max_chars=1200),
            source=lane["name"],
        )
    ]


def _classify_frontend_security(lane: dict[str, Any]) -> list[dict[str, str]]:
    if lane.get("status") == "passed":
        return [_make_finding(bucket="passed", summary="Frontend security checks passed", evidence="", source=lane["name"])]
    output = lane.get("output") or ""
    bucket = "review" if "critical" in output.lower() or "high" in output.lower() else "debt"
    return [
        _make_finding(
            bucket=bucket,
            summary="Frontend security checks reported findings",
            evidence=_trim_output(output, max_lines=16, max_chars=1200),
            source=lane["name"],
        )
    ]


def _lane_findings(lane: dict[str, Any]) -> list[dict[str, str]]:
    name = lane.get("name")
    if name == "backend-quality":
        return _classify_backend_quality(lane)
    if name == "backend-security":
        return _classify_backend_security(lane)
    if name == "frontend-lint":
        return _classify_frontend_lint(lane)
    if name == "frontend-build":
        return _classify_frontend_build(lane)
    if name == "frontend-security":
        return _classify_frontend_security(lane)
    if lane.get("status") == "passed":
        return [_make_finding(bucket="passed", summary=f"{name} passed", evidence="", source=str(name))]
    if lane.get("status") == "skipped":
        return [_make_finding(bucket="skipped", summary=f"{name} skipped", evidence="", source=str(name))]
    return [
        _make_finding(
            bucket="review",
            summary=f"{name} failed",
            evidence=_trim_output(lane.get("output") or "", max_lines=16, max_chars=1200),
            source=str(name),
        )
    ]


def _normalize_findings(lanes: list[dict[str, Any]]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for lane in lanes:
        findings.extend(_lane_findings(lane))
    return findings


def _bucket_counts(findings: list[dict[str, str]]) -> dict[str, int]:
    counts = {"blocking": 0, "review": 0, "debt": 0, "advisory": 0, "passed": 0, "skipped": 0}
    for finding in findings:
        bucket = finding.get("bucket", "review")
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _overall_decision(findings: list[dict[str, str]]) -> str:
    counts = _bucket_counts(findings)
    if counts["blocking"]:
        return "blocking"
    if counts["review"]:
        return "review"
    if counts["debt"]:
        return "debt"
    if counts["advisory"]:
        return "advisory"
    if counts["passed"]:
        return "passed"
    return "skipped"


def _normalized_summary(findings: list[dict[str, str]]) -> str:
    counts = _bucket_counts(findings)
    parts = []
    for bucket in ("blocking", "review", "debt", "advisory"):
        if counts[bucket]:
            parts.append(f"{bucket}={counts[bucket]}")
    if not parts and counts["passed"]:
        parts.append("passed")
    if not parts and counts["skipped"]:
        parts.append("skipped")
    return " ".join(parts) if parts else "no findings"


def _policy_lines() -> list[str]:
    return [
        "- `backend-quality / pytest fail` -> `blocking`",
        "- `frontend-build fail` -> `blocking`",
        "- `lint/ruff/eslint fail` -> `review`",
        "- `pip-audit` critical runtime packages -> `review`, remaining dependency backlog -> `debt`",
        "- `Bandit` high -> `blocking`, medium -> `review`, low -> `advisory`",
        "- `Semgrep` injection/auth/password rules -> `blocking`, weaker security smells -> `review`",
        "- `frontend security` findings -> `review` when severity words imply risk, otherwise `debt`",
    ]


def _render_report(result: dict[str, Any]) -> str:
    lines = [
        f"# Meridian Review Signals: {result['task_id']}",
        "",
        f"- scanned_at: {result.get('scanned_at', '')}",
        f"- workspace: `{result.get('workspace', '')}`",
        f"- executor: `{result.get('executor', '')}`",
        f"- status: `{result.get('status', '')}`",
        f"- decision: `{result.get('decision', '')}`",
        f"- summary: {result.get('summary', '')}",
        f"- normalized_summary: {result.get('normalized_summary', '')}",
    ]
    scope = result.get("scope") or {}
    if scope:
        lines.append(f"- scope_mode: `{scope.get('mode', '')}`")
        lines.append(f"- scope_reason: `{scope.get('reason', '')}`")
        if scope.get("paths"):
            lines.append(f"- scoped_paths: `{', '.join(scope.get('paths') or [])}`")
        if scope.get("selected_lanes"):
            lines.append(f"- selected_lanes: `{', '.join(scope.get('selected_lanes') or [])}`")
    if result.get("trigger_event_id"):
        lines.append(f"- trigger_event_id: `{result['trigger_event_id']}`")
    if result.get("triggered_by"):
        lines.append(f"- triggered_by: `{result['triggered_by']}`")
    findings = result.get("findings") or []
    if findings:
        lines.extend(["", "## Normalized Findings", ""])
        for finding in findings:
            lines.append(f"### [{finding.get('bucket')}] {finding.get('summary')}")
            lines.append("")
            lines.append(f"- source: `{finding.get('source')}`")
            if finding.get("evidence"):
                lines.extend(["", "```text", finding["evidence"], "```"])
            lines.append("")
    lines.extend(["", "## Severity Policy", ""])
    lines.extend(_policy_lines())
    lines.extend(["", "## Raw Lanes", ""])
    for lane in result.get("lanes", []):
        lines.append(
            f"### {lane.get('name')} [{lane.get('status')}]"
        )
        lines.append("")
        lines.append(f"- kind: `{lane.get('kind')}`")
        lines.append(f"- command: `{lane.get('command')}`")
        lines.append(f"- exit_code: `{lane.get('exit_code')}`")
        lines.append(f"- duration_seconds: `{lane.get('duration_seconds')}`")
        output = lane.get("output") or ""
        if output:
            lines.extend(["", "```text", output, "```"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_report(result: dict[str, Any]) -> str:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{result['task_id']}.md"
    report_path.write_text(_render_report(result), encoding="utf-8")
    return str(report_path)


def _task_result_summary(result: dict[str, Any]) -> str:
    findings = result.get("findings") or _normalize_findings(result.get("lanes") or [])
    return _normalized_summary(findings)


def latest_quality_result(task_id: str, *, state_path: Path = STATE_PATH) -> dict[str, Any] | None:
    state = _load_state(state_path)
    result = state.get("results", {}).get(task_id)
    return dict(result) if isinstance(result, dict) else None


def quality_brief_for_task(task_id: str, *, state_path: Path = STATE_PATH) -> str:
    result = latest_quality_result(task_id, state_path=state_path)
    if not result:
        return ""
    report_path = result.get("report_path") or "-"
    return (
        f"Quality gate: `{result.get('decision', result.get('status', 'unknown'))}`"
        f" | {result.get('normalized_summary', result.get('summary', _task_result_summary(result)))}"
        f" | report: `{report_path}`"
    )


def _scan_task(
    task_id: str,
    *,
    executor: Executor,
    triggered_by: str,
    trigger_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    metadata = _task_metadata_for_scope(executor, task_id)
    scope = _task_scope(executor, metadata)
    available_lanes = _available_lanes(executor)
    selected_lanes = _filter_lanes_for_scope(available_lanes, scope)
    scope = {
        **scope,
        "selected_lanes": [str(lane.get("name") or "") for lane in selected_lanes],
    }
    lanes: list[dict[str, Any]] = []
    for lane in selected_lanes:
        lane_started = time.monotonic()
        cwd = str(Path(executor.workspace) / lane["cwd"]) if executor.mode == "local" else str(Path(executor.workspace) / lane["cwd"])
        completed = _run_command(executor, lane["command"], cwd=cwd)
        combined_output = _trim_output("\n".join(part for part in (completed.stdout, completed.stderr) if part))
        lanes.append(
            {
                "name": lane["name"],
                "kind": lane["kind"],
                "command": lane["command"],
                "cwd": cwd,
                "status": "passed" if completed.returncode == 0 else "failed",
                "exit_code": completed.returncode,
                "duration_seconds": round(time.monotonic() - lane_started, 2),
                "output": combined_output,
            }
        )

    if not lanes:
        lanes.append(
            {
                "name": "scope-no-op" if scope.get("mode") == "docs_only" else "no-op",
                "kind": "quality",
                "command": "",
                "cwd": executor.workspace,
                "status": "skipped",
                "exit_code": 0,
                "duration_seconds": 0.0,
                "output": (
                    "Only non-code linked_paths were provided; skipping backend/frontend quality lanes."
                    if scope.get("mode") == "docs_only"
                    else "No supported backend/frontend quality lanes were detected in this workspace."
                ),
            }
        )

    summary = _summarize_lanes(lanes)
    status = "passed" if summary["failed"] == 0 else "failed"
    findings = _normalize_findings(lanes)
    decision = _overall_decision(findings)
    result = {
        "task_id": task_id,
        "workspace": executor.workspace,
        "executor": executor.mode if executor.mode == "local" else f"ssh:{executor.user}@{executor.host}",
        "trigger_event_id": trigger_event.get("id") if isinstance(trigger_event, dict) else None,
        "triggered_by": triggered_by,
        "scanned_at": _isoformat(_utcnow()),
        "duration_seconds": round(time.monotonic() - started, 2),
        "status": status,
        "decision": decision,
        "summary": _task_result_summary({"findings": findings}),
        "normalized_summary": _normalized_summary(findings),
        "findings": findings,
        "lanes": lanes,
        "scope": scope,
    }
    result["report_path"] = _write_report(result)
    return result


def _emit_result_event(result: dict[str, Any]) -> None:
    event_type = "meridian_quality_gate_completed" if result.get("status") == "passed" else "meridian_quality_gate_failed"
    emit_meridian_event(
        event_type,
        {
            "task_id": result.get("task_id"),
            "workspace": result.get("workspace"),
            "report_path": result.get("report_path"),
            "summary": result.get("summary"),
            "status": result.get("status"),
            "decision": result.get("decision"),
            "normalized_summary": result.get("normalized_summary"),
            "trigger_event_id": result.get("trigger_event_id"),
        },
    )


def _new_events(state: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        lines = EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    cursor = int(state.get("line_cursor") or 0)
    if cursor < 0 or cursor > len(lines):
        cursor = 0
    new_lines = lines[cursor:]
    state["line_cursor"] = len(lines)
    events: list[dict[str, Any]] = []
    for raw in new_lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _review_events(events: list[dict[str, Any]], state: dict[str, Any]) -> list[dict[str, Any]]:
    processed = set(str(item) for item in state.get("processed_event_ids") or [])
    selected: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event.get("id") or "")
        if event_id and event_id in processed:
            continue
        if event.get("type") not in DEFAULT_EVENT_TYPES:
            continue
        if event.get("to_queue") != DEFAULT_REVIEW_QUEUE:
            continue
        if not event.get("task_id"):
            continue
        selected.append(event)
    return selected


def _parse_task_frontmatter(content: str) -> dict[str, Any]:
    if content.startswith("---\n"):
        closing = content.find("\n---\n", 4)
        if closing != -1:
            raw = content[4:closing]
            try:
                import yaml

                payload = yaml.safe_load(raw) or {}
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _local_review_candidates(workspace: str) -> list[dict[str, str]]:
    root = Path(workspace) / "tasks" / "review"
    candidate_dirs = [root / "active", root]
    candidates: list[dict[str, str]] = []
    seen_task_ids: set[str] = set()
    for review_dir in candidate_dirs:
        if not review_dir.exists():
            continue
        for path in sorted(review_dir.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                metadata = _parse_task_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if review_dir == root and is_review_decision_artifact(path, metadata):
                continue
            task_id = str(metadata.get("id") or path.stem)
            if task_id in seen_task_ids:
                continue
            seen_task_ids.add(task_id)
            candidates.append(
                {
                    "task_id": task_id,
                    "transition_at": str(metadata.get("last_transition_at") or metadata.get("updated_at") or ""),
                }
            )
    return candidates


def _ssh_review_candidates(executor: Executor) -> list[dict[str, str]]:
    script = """
import json
from pathlib import Path
import yaml

workspace = Path(__WORKSPACE__).expanduser()
review_root = workspace / "tasks" / "review"
payload = []
seen = set()
for review_dir in (review_root / "active", review_root):
    if not review_dir.exists():
        continue
    for path in sorted(review_dir.iterdir()):
        if not path.is_file() or path.name.startswith('.'):
            continue
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            continue
        metadata = {}
        if content.startswith('---\\n'):
            closing = content.find('\\n---\\n', 4)
            if closing != -1:
                loaded = yaml.safe_load(content[4:closing]) or {}
                if isinstance(loaded, dict):
                    metadata = loaded
        kind = str(metadata.get('review_kind') or '').strip().lower()
        if review_dir == review_root and kind == 'decision':
            continue
        task_id = str(metadata.get('id') or path.stem)
        if task_id in seen:
            continue
        seen.add(task_id)
        payload.append({
            'task_id': task_id,
            'transition_at': str(metadata.get('last_transition_at') or metadata.get('updated_at') or ''),
        })
print(json.dumps(payload, ensure_ascii=False))
""".replace("__WORKSPACE__", repr(executor.workspace))
    result = _run_command(
        executor,
        f"python3 - <<'PY'\n{script}\nPY",
        cwd=executor.workspace,
        timeout=45,
    )
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []
    candidates: list[dict[str, str]] = []
    for item in payload:
        if isinstance(item, dict) and item.get("task_id"):
            candidates.append(
                {
                    "task_id": str(item.get("task_id")),
                    "transition_at": str(item.get("transition_at") or ""),
                }
            )
    return candidates


def _review_candidates_from_workspace(executor: Executor) -> list[dict[str, str]]:
    if executor.mode == "ssh":
        return _ssh_review_candidates(executor)
    return _local_review_candidates(executor.workspace)


def _needs_rescan(task_id: str, transition_at: str, state: dict[str, Any]) -> bool:
    result = state.get("results", {}).get(task_id)
    if not isinstance(result, dict):
        return True
    scanned_at = parse_iso_datetime(result.get("scanned_at"))
    transitioned_at = parse_iso_datetime(transition_at)
    if transitioned_at and scanned_at:
        return scanned_at < transitioned_at
    return False


def run_quality_gate_once(
    workspace: str | Path | None = None,
    *,
    task_id: str | None = None,
    force: bool = False,
    state_path: Path = STATE_PATH,
) -> dict[str, Any]:
    state = _load_state(state_path)
    executor = _build_executor(workspace)
    results: list[dict[str, Any]] = []

    if task_id:
        result = _scan_task(task_id, executor=executor, triggered_by="manual")
        state["results"][task_id] = result
        results.append(result)
        _emit_result_event(result)
        _save_state(state, state_path)
        return {"scanned": results, "count": len(results), "source": "manual"}

    events = _new_events(state)
    review_events = _review_events(events, state)
    if not task_id:
        for candidate in _review_candidates_from_workspace(executor):
            if not _needs_rescan(candidate["task_id"], candidate.get("transition_at", ""), state):
                continue
            if any(item.get("task_id") == candidate["task_id"] for item in review_events):
                continue
            review_events.append(
                {
                    "id": "",
                    "task_id": candidate["task_id"],
                    "type": "review_queue_detected",
                    "transition_at": candidate.get("transition_at", ""),
                }
            )
    if force and not review_events:
        last_review_task = None
        for event in reversed(events):
            if event.get("to_queue") == DEFAULT_REVIEW_QUEUE and event.get("task_id"):
                last_review_task = event.get("task_id")
                break
        if last_review_task:
            review_events = [{"id": "", "task_id": last_review_task, "type": "manual_force"}]

    for event in review_events:
        result = _scan_task(
            str(event["task_id"]),
            executor=executor,
            triggered_by=str(event.get("type") or "event"),
            trigger_event=event,
        )
        state["results"][str(event["task_id"])] = result
        results.append(result)
        event_id = str(event.get("id") or "")
        if event_id:
            processed = list(state.get("processed_event_ids") or [])
            processed.append(event_id)
            state["processed_event_ids"] = processed[-200:]
        _emit_result_event(result)

    _save_state(state, state_path)
    return {"scanned": results, "count": len(results), "source": "event"}


def format_quality_status(task_id: str | None = None, *, state_path: Path = STATE_PATH) -> str:
    state = _load_state(state_path)
    results = state.get("results") or {}
    if task_id:
        result = results.get(task_id)
        if not isinstance(result, dict):
            return f"No quality-gate result recorded for `{task_id}`."
        lines = [
            f"Meridian quality gate `{task_id}`",
            f"  Status:   {result.get('status')}",
            f"  Decision: {result.get('decision')}",
            f"  Summary:  {result.get('normalized_summary', result.get('summary'))}",
            f"  Scanned:  {result.get('scanned_at')}",
            f"  Report:   {result.get('report_path')}",
        ]
        for finding in result.get("findings", [])[:8]:
            lines.append(
                f"  - [{finding.get('bucket')}] {finding.get('summary')}"
            )
        return "\n".join(lines)

    ordered = sorted(
        (
            item
            for item in results.values()
            if isinstance(item, dict)
        ),
        key=lambda item: parse_iso_datetime(item.get("scanned_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if not ordered:
        return "No quality-gate results recorded yet."
    aggregate = {"blocking": 0, "review": 0, "debt": 0, "advisory": 0}
    for item in ordered:
        counts = _bucket_counts(item.get("findings") or [])
        for bucket in aggregate:
            if counts.get(bucket):
                aggregate[bucket] += 1
    aggregate_parts = [f"{bucket}={count}" for bucket, count in aggregate.items() if count]
    lines = ["Meridian quality gate", f"  Recorded tasks: {len(ordered)}"]
    if aggregate_parts:
        lines.append(f"  Aggregate: {' '.join(aggregate_parts)}")
    for item in ordered[:8]:
        lines.append(
            f"  - {item.get('task_id')}: {item.get('decision')} | "
            f"{item.get('normalized_summary', item.get('summary'))} | {item.get('scanned_at')}"
        )
    return "\n".join(lines)
