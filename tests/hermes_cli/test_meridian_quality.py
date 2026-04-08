from __future__ import annotations

from pathlib import Path

import yaml

from hermes_cli.meridian_quality import (
    Executor,
    _filter_lanes_for_scope,
    _git_changed_paths,
    _local_review_candidates,
    _scan_task,
    _task_scope,
    _task_scope_from_metadata,
)


def _write_review_file(path: Path, metadata: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(metadata, sort_keys=False).strip() + "\n---\n",
        encoding="utf-8",
    )


def test_local_review_candidates_prefer_active_and_skip_decision_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    _write_review_file(
        workspace / "tasks" / "review" / "active" / "task-active.md",
        {"id": "TASK-ACTIVE", "updated_at": "2026-04-08T01:00:00+00:00"},
    )
    _write_review_file(
        workspace / "tasks" / "review" / "TASK-ACTIVE-legacy.md",
        {"id": "TASK-ACTIVE", "updated_at": "2026-04-08T00:00:00+00:00"},
    )
    _write_review_file(
        workspace / "tasks" / "review" / "TASK-DECISION.md",
        {
            "review_schema_version": 1,
            "review_task_id": "TASK-ACTIVE",
            "review_kind": "decision",
            "review_outcome": "approved",
            "decision_bucket": "passed",
            "reviewer": "matthew",
            "status": "final",
            "required_actions": [],
            "updated_at": "2026-04-08T02:00:00+00:00",
        },
    )

    candidates = _local_review_candidates(str(workspace))

    assert candidates == [{"task_id": "TASK-ACTIVE", "transition_at": "2026-04-08T01:00:00+00:00"}]


def test_task_scope_from_metadata_selects_backend_only():
    scope = _task_scope_from_metadata(
        {
            "linked_files": ["backend/apps/workspaces/services.py"],
            "linked_dirs": [],
        }
    )

    assert scope["mode"] == "scoped"
    assert scope["categories"] == ["backend"]


def test_task_scope_from_metadata_selects_mixed_frontend_backend():
    scope = _task_scope_from_metadata(
        {
            "linked_files": ["frontend/src/store/mapStore.ts", "backend/config/settings.py"],
        }
    )

    assert scope["mode"] == "scoped"
    assert set(scope["categories"]) == {"backend", "frontend"}


def test_task_scope_from_metadata_marks_docs_only_scope():
    scope = _task_scope_from_metadata(
        {
            "linked_files": ["docs/llm/testing-strategy.md", "tasks/review/TASK-1.md"],
        }
    )

    assert scope["mode"] == "docs_only"


def test_filter_lanes_for_scope_limits_frontend_lanes():
    lanes = [
        {"name": "backend-quality"},
        {"name": "backend-security"},
        {"name": "frontend-lint"},
        {"name": "frontend-build"},
        {"name": "frontend-security"},
    ]

    selected = _filter_lanes_for_scope(
        lanes,
        {"mode": "scoped", "categories": ["frontend"]},
    )

    assert [lane["name"] for lane in selected] == ["frontend-lint", "frontend-build", "frontend-security"]


def test_git_changed_paths_prefers_branch_diff(monkeypatch):
    executor = Executor(mode="local", workspace="/tmp/workspace")
    seen_commands: list[str] = []

    class _Result:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def _fake_run_command(_executor, command, *, cwd, timeout=1800):
        seen_commands.append(command)
        if "origin/main...task/frontend-sync" in command:
            return _Result(0, "frontend/src/store/mapStore.ts\n")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("hermes_cli.meridian_quality._run_command", _fake_run_command)

    paths, reason = _git_changed_paths(executor, {"pr_branch": "task/frontend-sync"})

    assert paths == ["frontend/src/store/mapStore.ts"]
    assert reason == "git_branch_diff"
    assert seen_commands == ["git diff --name-only --relative origin/main...task/frontend-sync"]


def test_task_scope_falls_back_to_git_diff_when_metadata_has_no_linked_paths(monkeypatch):
    executor = Executor(mode="local", workspace="/tmp/workspace")
    monkeypatch.setattr(
        "hermes_cli.meridian_quality._run_command",
        lambda _executor, command, *, cwd, timeout=1800: type(
            "_Result",
            (),
            {
                "returncode": 0,
                "stdout": "backend/apps/workspaces/services.py\n",
                "stderr": "",
            },
        )(),
    )

    scope = _task_scope(executor, {"pr_branch": "task/backend-fix"})

    assert scope["mode"] == "scoped"
    assert scope["reason"] == "git_branch_diff"
    assert scope["categories"] == ["backend"]


def test_task_scope_keeps_full_scan_when_git_diff_yields_nothing(monkeypatch):
    executor = Executor(mode="local", workspace="/tmp/workspace")
    monkeypatch.setattr(
        "hermes_cli.meridian_quality._run_command",
        lambda _executor, command, *, cwd, timeout=1800: type(
            "_Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "",
            },
        )(),
    )

    scope = _task_scope(executor, {"pr_branch": "task/unknown"})

    assert scope["mode"] == "full"
    assert scope["reason"] == "no_scope_metadata"


def test_scan_task_skips_heavy_lanes_for_docs_only_scope(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = Executor(mode="local", workspace=str(workspace))
    monkeypatch.setattr("hermes_cli.meridian_quality.REPORTS_DIR", tmp_path / "reports")
    task_path = workspace / "tasks" / "review" / "active" / "task-1.md"
    _write_review_file(
        task_path,
        {"id": "TASK-1", "linked_files": ["docs/llm/testing-strategy.md"]},
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_quality._available_lanes",
        lambda _executor: [{"name": "backend-quality", "kind": "quality", "cwd": ".", "command": "echo hi"}],
    )
    called = {"count": 0}

    def _fake_run_command(_executor, _command, *, cwd, timeout=1800):
        called["count"] += 1
        raise AssertionError("docs-only scope should not execute heavy lanes")

    monkeypatch.setattr("hermes_cli.meridian_quality._run_command", _fake_run_command)

    result = _scan_task("TASK-1", executor=executor, triggered_by="manual")

    assert called["count"] == 0
    assert result["scope"]["mode"] == "docs_only"
    assert result["lanes"][0]["status"] == "skipped"
    assert result["lanes"][0]["name"] == "scope-no-op"


def test_scan_task_runs_only_frontend_lanes_when_scope_is_frontend(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = Executor(mode="local", workspace=str(workspace))
    monkeypatch.setattr("hermes_cli.meridian_quality.REPORTS_DIR", tmp_path / "reports")
    task_path = workspace / "tasks" / "review" / "active" / "task-1.md"
    _write_review_file(
        task_path,
        {"id": "TASK-1", "linked_files": ["frontend/src/store/mapStore.ts"]},
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_quality._available_lanes",
        lambda _executor: [
            {"name": "backend-quality", "kind": "quality", "cwd": ".", "command": "backend"},
            {"name": "frontend-lint", "kind": "quality", "cwd": "frontend", "command": "frontend-lint"},
            {"name": "frontend-build", "kind": "quality", "cwd": "frontend", "command": "frontend-build"},
        ],
    )

    executed: list[str] = []

    class _Result:
        def __init__(self):
            self.returncode = 0
            self.stdout = "ok"
            self.stderr = ""

    def _fake_run_command(_executor, command, *, cwd, timeout=1800):
        executed.append(command)
        return _Result()

    monkeypatch.setattr("hermes_cli.meridian_quality._run_command", _fake_run_command)

    result = _scan_task("TASK-1", executor=executor, triggered_by="manual")

    assert executed == ["frontend-lint", "frontend-build"]
    assert result["scope"]["selected_lanes"] == ["frontend-lint", "frontend-build"]


def test_scan_task_uses_git_diff_fallback_for_lane_selection(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = Executor(mode="local", workspace=str(workspace))
    monkeypatch.setattr("hermes_cli.meridian_quality.REPORTS_DIR", tmp_path / "reports")
    task_path = workspace / "tasks" / "review" / "active" / "task-1.md"
    _write_review_file(
        task_path,
        {"id": "TASK-1", "pr_branch": "task/frontend-only"},
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_quality._available_lanes",
        lambda _executor: [
            {"name": "backend-quality", "kind": "quality", "cwd": ".", "command": "backend"},
            {"name": "frontend-lint", "kind": "quality", "cwd": "frontend", "command": "frontend-lint"},
            {"name": "frontend-build", "kind": "quality", "cwd": "frontend", "command": "frontend-build"},
        ],
    )

    executed: list[str] = []

    class _Result:
        def __init__(self, returncode=0, stdout="ok", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def _fake_run_command(_executor, command, *, cwd, timeout=1800):
        if command.startswith("git diff --name-only --relative "):
            return _Result(stdout="frontend/src/store/mapStore.ts\n")
        executed.append(command)
        return _Result()

    monkeypatch.setattr("hermes_cli.meridian_quality._run_command", _fake_run_command)

    result = _scan_task("TASK-1", executor=executor, triggered_by="manual")

    assert executed == ["frontend-lint", "frontend-build"]
    assert result["scope"]["reason"] == "git_branch_diff"
    assert result["scope"]["selected_lanes"] == ["frontend-lint", "frontend-build"]
