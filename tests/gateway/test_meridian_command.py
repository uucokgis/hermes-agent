from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:c1:u1",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = []
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._background_tasks = set()
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._clear_session_env = lambda: None
    return runner


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            user_name="umut",
            chat_type="dm",
        ),
        message_id="m1",
    )


def test_meridian_status_command_returns_role_summary(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(
        "hermes_cli.meridian_support.build_roles_status_text",
        lambda: "Meridian role summary",
    )

    result = asyncio.run(runner._handle_meridian_command(_make_event("/meridian status")))
    assert result == "Meridian role summary"


def test_meridian_default_command_returns_menu(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(runner, "_meridian_menu_text", lambda source: "menu text")

    result = asyncio.run(runner._handle_meridian_command(_make_event("/meridian")))

    assert result == "menu text"


def test_meridian_waiting_command_returns_waiting_summary(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(runner, "_meridian_waiting_text", lambda: "waiting summary")

    result = asyncio.run(runner._handle_meridian_command(_make_event("/meridian waiting")))

    assert result == "waiting summary"


def test_meridian_cancel_clears_pending_flow():
    runner = _make_runner()
    runner._pending_meridian_flows = {"agent:main:telegram:dm:c1:u1": {"kind": "ask", "step": "role", "created_at": 1.0}}

    result = runner._handle_meridian_cancel_command("agent:main:telegram:dm:c1:u1")

    assert "Cancelled" in result
    assert runner._pending_meridian_flows == {}


def test_meridian_watch_command_starts_watcher(monkeypatch):
    runner = _make_runner()
    runner._background_tasks = set()
    called = {}

    async def _fake_start(source, role):
        called["role"] = role
        return f"watching {role}"

    runner._start_meridian_watch = _fake_start

    result = asyncio.run(runner._handle_meridian_watch_command(_make_event("/meridian_watch fatih")))

    assert result == "watching fatih"
    assert called["role"] == "fatih"


def test_meridian_watch_status_reports_active_roles():
    runner = _make_runner()
    runner._meridian_log_watchers = {
        "telegram:c1:fatih": object(),
        "telegram:c1:matthew": object(),
    }

    result = asyncio.run(runner._handle_meridian_watch_command(_make_event("/meridian_watch status")))

    assert "fatih" in result
    assert "matthew" in result


def test_meridian_unwatch_command_stops_specific_watcher():
    runner = _make_runner()
    called = {}

    async def _fake_stop(source, role):
        called["role"] = role
        return f"stopped {role}"

    runner._stop_meridian_watch = _fake_stop

    result = asyncio.run(runner._handle_meridian_unwatch_command(_make_event("/meridian_unwatch fatih")))

    assert result == "stopped fatih"
    assert called["role"] == "fatih"


def test_meridian_ask_command_creates_ticket(monkeypatch):
    runner = _make_runner()
    fake_ticket = SimpleNamespace(ticket_id="20260407001")
    monkeypatch.setattr(
        "hermes_cli.meridian_support.create_support_ticket",
        lambda **_kwargs: fake_ticket,
    )

    result = asyncio.run(
        runner._handle_meridian_ask_command(
            _make_event("/meridian_ask fatih drawing editor issue")
        )
    )

    assert "20260407001" in result
    assert "fatih" in result


def test_meridian_reply_command_appends_human_reply(monkeypatch):
    runner = _make_runner()
    existing = SimpleNamespace(ticket_id="20260407001")
    updated = SimpleNamespace(
        ticket_id="20260407001",
        metadata={"target_role": "matthew", "status": "human_replied"},
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_support.get_support_ticket",
        lambda _ticket_id: existing,
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_support.append_human_reply",
        lambda *_args, **_kwargs: updated,
    )

    result = asyncio.run(
        runner._handle_meridian_reply_command(
            _make_event("/meridian_reply 20260407001 please focus on package risk first")
        )
    )

    assert "20260407001" in result
    assert "matthew" in result


def test_meridian_reply_command_without_args_prompts_with_waiting_tickets(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(
        runner,
        "_tickets_waiting_on_human",
        lambda: [
            SimpleNamespace(ticket_id="20260407001", status="pending_role", queue="inbox", summary="Need answer")
        ],
    )

    result = asyncio.run(runner._handle_meridian_reply_command(_make_event("/meridian_reply")))

    assert "20260407001" in result
    assert "Yanıtlamak istediğin ticket ID" in result


def test_meridian_task_command_without_args_prompts_waiting_tasks(monkeypatch):
    runner = _make_runner()
    monkeypatch.setattr(runner, "_meridian_waiting_task_prompt", lambda: "task prompt")

    result = asyncio.run(runner._handle_meridian_task_command(_make_event("/meridian_task")))

    assert result == "task prompt"


def test_meridian_task_detail_includes_review_and_quality_briefs(tmp_path, monkeypatch):
    runner = _make_runner()
    workspace = tmp_path / "workspace"
    (workspace / "tasks" / "review").mkdir(parents=True, exist_ok=True)
    task_path = workspace / "tasks" / "review" / "task-1.md"
    task_path.write_text(
        "---\n"
        "id: TASK-1\n"
        "title: Task 1\n"
        "status: review\n"
        "updated_at: 2026-04-08T01:00:00+00:00\n"
        "---\n",
        encoding="utf-8",
    )
    review_dir = workspace / "tasks" / "review" / "decisions"
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "TASK-1-decision.md").write_text(
        "---\n"
        "review_schema_version: 1\n"
        "review_id: REVIEW-20260408-001\n"
        "review_task_id: TASK-1\n"
        "review_kind: decision\n"
        "review_outcome: request_changes\n"
        "decision_bucket: blocking\n"
        "reviewer: matthew\n"
        "status: final\n"
        "required_actions:\n"
        "  - id: RA-1\n"
        "    summary: Fix regression\n"
        "    status: open\n"
        "updated_at: 2026-04-08T02:00:00+00:00\n"
        "summary: Regression found during review\n"
        "---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_support.resolve_support_workspace",
        lambda _workspace=None: workspace,
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_quality.quality_brief_for_task",
        lambda _task_id: "Quality gate: `review` | review=1",
    )

    result = runner._meridian_task_detail("TASK-1")

    assert "Quality gate: `review`" in result
    assert "Review decision: `request_changes`" in result
    assert "open_actions: `1`" in result


def test_pending_meridian_ask_flow_collects_role_then_message(monkeypatch):
    runner = _make_runner()
    runner._pending_meridian_flows = {"agent:main:telegram:dm:c1:u1": {"kind": "ask", "step": "role", "created_at": 9999999999}}
    monkeypatch.setattr(
        "hermes_cli.meridian_support.create_support_ticket",
        lambda **_kwargs: SimpleNamespace(ticket_id="20260407001"),
    )

    first = asyncio.run(runner._handle_pending_meridian_flow(_make_event("fatih"), "agent:main:telegram:dm:c1:u1"))
    second = asyncio.run(runner._handle_pending_meridian_flow(_make_event("please check map toolbar"), "agent:main:telegram:dm:c1:u1"))

    assert "mesajı yaz" in first
    assert "20260407001" in second


def test_pending_meridian_flow_times_out():
    runner = _make_runner()
    runner._pending_meridian_flows = {"agent:main:telegram:dm:c1:u1": {"kind": "ask", "step": "role", "created_at": 0.0}}

    result = asyncio.run(runner._handle_pending_meridian_flow(_make_event("fatih"), "agent:main:telegram:dm:c1:u1"))

    assert "timed out" in result


def test_meridian_watch_filter_drops_separator_noise():
    runner = _make_runner()

    lines = runner._filter_meridian_watch_lines(
        "\n".join(
            [
                "-----",
                "=======",
                "Profile exists: meridian-fatih",
                "preparing workspace",
                "Meaningful update",
                "Meaningful update",
                "task moved to review",
            ]
        )
    )

    assert lines == ["Meaningful update", "task moved to review"]
