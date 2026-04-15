#!/usr/bin/env python3
"""Meridian waiting-human Telegram notifier wrapper.
Outputs a formatted message only when there are new or existing waiting_human items."""

import sys
sys.path.insert(0, "/home/umut/Hermes-Agent")
from pathlib import Path
from hermes_cli.meridian_notifier import run_waiting_human_notifier

state_path = Path.home() / ".hermes" / "meridian" / "waiting_human_notifier_state.json"
result = run_waiting_human_notifier(state_path=state_path)

if result["has_waiting_human"] or result["has_support_waiting"]:
    print(result["brief"])
    if result["changed"]:
        print("\n(yeni degisiklik tespit edildi)")
# Bekleme yoksa sessiz kal
