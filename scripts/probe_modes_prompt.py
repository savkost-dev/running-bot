"""Печать полного собранного промта Шага 1.5 (режимы брифа).
Запуск: python scripts/probe_modes_prompt.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import announce_brief  # noqa: E402

print(announce_brief._MODES_PROMPT)
