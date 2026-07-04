from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _extract_pattern(script: str) -> str:
    match = re.search(r'grep -E "([^"]*\$\{?TOKEN\}?[^"]*)"', script)
    assert match, "poller TOKEN grep pattern not found"
    return (
        match.group(1)
        .replace("${TOKEN}", re.escape("NEEDS_HERMES_WATCHDOG:"))
        .replace("$TOKEN", re.escape("NEEDS_HERMES_WATCHDOG:"))
    )


def test_watchdog_poller_accepts_decorated_claude_tui_sentinel():
    runtime_path = Path("/tmp/watch-claude-watchdog.sh")
    repo_path = Path(__file__).resolve().parents[2] / "scripts" / "watch-claude-watchdog.sh"
    script_path = runtime_path if runtime_path.exists() else repo_path
    assert script_path.exists(), "watchdog poller script must exist in /tmp or repo scripts/"
    pattern = _extract_pattern(script_path.read_text())

    sample = "\n".join([
        "⏺ NEEDS_HERMES_WATCHDOG: deliver escalation",
        "  NEEDS_HERMES_WATCHDOG: deliver escalation",
        "● NEEDS_HERMES_WATCHDOG: deliver escalation",
        "prose mentions NEEDS_HERMES_WATCHDOG: without line sentinel",
    ]) + "\n"
    proc = subprocess.run(["grep", "-E", pattern], input=sample, text=True, capture_output=True, check=True)
    lines = proc.stdout.splitlines()
    assert "⏺ NEEDS_HERMES_WATCHDOG: deliver escalation" in lines
    assert "  NEEDS_HERMES_WATCHDOG: deliver escalation" in lines
    assert "● NEEDS_HERMES_WATCHDOG: deliver escalation" in lines
    assert "prose mentions NEEDS_HERMES_WATCHDOG: without line sentinel" not in lines
