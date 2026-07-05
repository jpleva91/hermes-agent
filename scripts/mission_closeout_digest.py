#!/usr/bin/env python3
"""Mission Engine closeout digest — the missing "vault brief + Discord digest"
half of the Spec 002 acceptance loop.

When a reviewed mission origin card reaches `done`, this composes a closeout
digest and (with --send) delivers it to Discord via the sanctioned send path.
Detection is idempotent via a state file; the first run should be `--init` to
seed the baseline so historical completions are NOT re-announced.

Safety mirrors the reactive sweep: DRY-RUN by default. `--send` is the only mode
that posts to Discord. Nothing here mutates kanban state.

    python scripts/mission_closeout_digest.py --init          # seed baseline, send nothing
    python scripts/mission_closeout_digest.py                 # dry-run: show what WOULD be sent
    python scripts/mission_closeout_digest.py --send          # deliver new closeouts to Discord
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_cli import kanban_db as kb  # noqa: E402
from scripts import mission_engine_reactive_sweep as sweep  # noqa: E402

BOARD = sweep.BOARD
GATEWARDEN = sweep.GATEWARDEN
def _state_path() -> Path:
    # Resolved at call time so it follows HERMES_HOME / Path.home() (test isolation).
    return Path.home() / ".hermes" / "mission-engine" / "closeout-digests.json"
# Default target: the #clawta channel (same channel D015 and the sweep cron use).
# Override with --target (e.g. a specific mission thread). Format: discord:<channel_id>[:<thread_id>].
DEFAULT_TARGET = "discord:1508984141197213728"

# Sub-work cards (gates, cures, re-gates) are never mission origins; they are the
# machinery of a mission, not a mission closeout.
_SUBWORK_TITLE_RE = re.compile(
    r"^\s*(?:cure|re-?gate|gate|ready gate|gate review|acceptance gate)\s*[:\-]",
    re.IGNORECASE,
)


def _rows(conn, sql: str, params=()):
    return list(conn.execute(sql, params).fetchall())


def _went_through_gate(conn, card_id: str) -> bool:
    """True if some done gatewarden gate references this card — i.e. it was
    actually reviewed, distinguishing a real mission from a trivial done card."""
    for r in _rows(
        conn,
        "SELECT title, body FROM tasks WHERE assignee = ? AND status = 'done'",
        (GATEWARDEN,),
    ):
        if card_id in f"{r['title'] or ''} {r['body'] or ''}":
            return True
    return False


def _is_mission_closeout(conn, row) -> bool:
    """A done, reviewed ORIGIN card (lineage root of itself, not gate/cure sub-work)."""
    if str(row["status"]) != "done":
        return False
    if str(row["assignee"] or "") == GATEWARDEN:
        return False
    if _SUBWORK_TITLE_RE.match(str(row["title"] or "")):
        return False
    if sweep._lineage_root(conn, row["id"]) != row["id"]:
        return False
    return _went_through_gate(conn, row["id"])


def detect_closeouts(conn, digested: set[str]) -> list[Any]:
    """New mission-closeout origin cards not yet digested, oldest-completed first."""
    rows = _rows(
        conn,
        "SELECT * FROM tasks WHERE status = 'done' ORDER BY completed_at ASC, created_at ASC",
    )
    return [r for r in rows if r["id"] not in digested and _is_mission_closeout(conn, r)]


def _approving_gate(conn, card_id: str):
    """The most recent done gatewarden gate in this card's lineage, for the verdict line."""
    best = None
    for r in _rows(
        conn,
        "SELECT * FROM tasks WHERE assignee = ? AND status = 'done' ORDER BY completed_at DESC",
        (GATEWARDEN,),
    ):
        if card_id in f"{r['title'] or ''} {r['body'] or ''}":
            best = r
            break
    return best


def compose_digest(conn, row) -> str:
    title = str(row["title"] or "").strip()
    gate = _approving_gate(conn, row["id"])
    verdict = "APPROVE" if (gate and sweep._is_approve_verdict(conn, gate)) else "(no gate verdict found)"
    gate_line = f"gate `{gate['id']}` → {verdict}" if gate else "no gate on lineage"
    health = _board_health(conn)
    return (
        f"🟢 Mission Engine — Closeout Digest\n\n"
        f"**Mission closed:** {title}\n"
        f"• Card `{row['id']}` reached done\n"
        f"• Closed via {gate_line}\n"
        f"• Board `{BOARD}`: {health}\n\n"
        f"_Auto-generated on mission convergence. Reply here to open a follow-up._"
    )


def _board_health(conn) -> str:
    actions = sweep.plan_actions(conn)
    props = sweep.plan_approve_propagations(conn)
    if actions or props:
        return f"active ({len(actions) + len(props)} sweep action(s) pending)"
    stalls = sweep.detect_stalls(conn)
    return "converged, 0 stalls" if not stalls else f"stalled, {len(stalls)} card(s) awaiting human"


def _load_state() -> dict:
    path = _state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"digested": [], "initialized": False}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _deliver(text: str, target: str) -> dict:
    """Post via the sanctioned send path. Imported lazily so dry-run/tests need no gateway deps."""
    from tools.send_message_tool import send_message_tool

    return send_message_tool({"action": "send", "target": target, "message": text})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mission Engine closeout digest → Discord")
    parser.add_argument("--board", default=BOARD)
    parser.add_argument("--init", action="store_true", help="Seed baseline from current done cards; send nothing.")
    parser.add_argument("--send", action="store_true", help="Deliver new closeouts to Discord. Omit for dry-run.")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="Discord send target (discord:channel:thread).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.board != BOARD:
        raise SystemExit(f"This digest is scoped to {BOARD!r}; got {args.board!r}")

    conn = kb.connect(board=BOARD)
    state = _load_state()
    digested = set(state.get("digested", []))

    if args.init:
        all_done = [
            r["id"]
            for r in _rows(conn, "SELECT * FROM tasks WHERE status = 'done'")
            if _is_mission_closeout(conn, r)
        ]
        state = {"digested": sorted(set(digested) | set(all_done)), "initialized": True}
        _save_state(state)
        out = {"mode": "init", "seeded": len(all_done), "total_digested": len(state["digested"])}
        print(json.dumps(out, indent=2) if args.json else f"init: seeded {len(all_done)} done mission cards; digest baseline set.")
        return 0

    closeouts = detect_closeouts(conn, digested)
    results = []
    for row in closeouts:
        text = compose_digest(conn, row)
        entry = {"id": row["id"], "title": str(row["title"] or "")[:80]}
        if args.send:
            res = _deliver(text, args.target)
            entry["delivered"] = bool(res.get("success"))
            entry["send_result"] = res
            if res.get("success"):
                digested.add(row["id"])
        else:
            entry["digest"] = text
        results.append(entry)

    if args.send:
        state["digested"] = sorted(digested)
        _save_state(state)

    out = {
        "mode": "send" if args.send else "dry-run",
        "board": BOARD,
        "new_closeouts": len(closeouts),
        "results": results,
    }
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
    elif not closeouts:
        print("closeout digest: no new mission closeouts.")
    else:
        print(f"closeout digest: {len(closeouts)} new closeout(s) [{out['mode']}]")
        for e in results:
            print(f"  {e['id']} | {e['title']}" + (f" | delivered={e.get('delivered')}" if args.send else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
