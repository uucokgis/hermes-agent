#!/usr/bin/env python3
"""Emit a compact waiting_human reminder for Meridian cron jobs."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.meridian_notifier import run_waiting_human_notifier


def main() -> int:
    state_path = Path.home() / ".hermes" / "meridian" / "waiting_human_notifier_state.json"
    result = run_waiting_human_notifier(state_path=state_path)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
