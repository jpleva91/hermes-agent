#!/usr/bin/env bash
set -euo pipefail

# Poll Claude Code / terminal transcript text from stdin for the Hermes watchdog
# sentinel. The sentinel may be emitted as a plain line or behind a TUI bullet
# / decorated prefix such as "⏺ NEEDS_HERMES_WATCHDOG:".
TOKEN="NEEDS_HERMES_WATCHDOG:"

grep -E "^[[:space:][:punct:]]*${TOKEN}" "$@"
