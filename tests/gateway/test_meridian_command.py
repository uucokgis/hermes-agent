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


def test_meridian_ticket_new_creates_ticket(monkeypatch):
    runner = _make_runner()
    fake_ticket = SimpleNamespace(ticket_id="20260407001")
    monkeypatch.setattr(
        "hermes_cli.meridian_support.create_support_ticket",
        lambda **_kwargs: fake_ticket,
    )

    result = asyncio.run(
        runner._handle_meridian_command(
            _make_event("/meridian ticket new fatih drawing editor issue")
        )
    )

    assert "20260407001" in result
    assert "fatih" in result


def test_meridian_ticket_reply_appends_human_reply(monkeypatch):
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
        runner._handle_meridian_command(
            _make_event("/meridian ticket 20260407001 please focus on package risk first")
        )
    )

    assert "20260407001" in result
    assert "matthew" in result


def test_meridian_ticket_show_formats_existing_ticket(monkeypatch):
    runner = _make_runner()
    ticket = SimpleNamespace(ticket_id="20260407001")
    monkeypatch.setattr(
        "hermes_cli.meridian_support.get_support_ticket",
        lambda _ticket_id: ticket,
    )
    monkeypatch.setattr(
        "hermes_cli.meridian_support.format_ticket_detail",
        lambda _ticket: "ticket detail",
    )

    result = asyncio.run(
        runner._handle_meridian_command(
            _make_event("/meridian ticket 20260407001")
        )
    )

    assert result == "ticket detail"
