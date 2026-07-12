"""Metered-ledger coverage checks for Mission Engine task runs.

``hermes stamp verify`` is the cheap guardrail for the post-subscription-flip
world: completed/running task runs must carry first-class lane/cost stamps, not
only prose hidden in handoff summaries. The command is intentionally read-only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as kb

REQUIRED_COLUMNS = (
    "model",
    "tokens_in",
    "tokens_out",
    "cached_tokens",
    "cost_usd",
    "billing_mode",
    "wall_clock_seconds",
)

_TERMINAL_STATUSES = ("done", "blocked", "crashed", "timed_out", "failed")


@dataclass
class StampCoverage:
    board: str
    checked_runs: int
    stamped_runs: int
    missing_runs: int
    coverage: float
    required_columns: list[str]
    missing_columns: list[str]
    sample_missing: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return not self.missing_columns and self.missing_runs == 0


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _task_run_columns(conn: sqlite3.Connection) -> set[str]:
    if not _has_table(conn, "task_runs"):
        return set()
    return {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}


def _db_path_for_board(board: str) -> Path:
    """Return ``board``'s DB path without inheriting a worker DB pin.

    Kanban workers run with ``HERMES_KANBAN_DB`` set so normal task commands
    cannot accidentally drift to another board. ``stamp verify`` is different:
    its output labels coverage by board, so an explicit/all-board verification
    must query the path for that label, not whatever DB the worker env pinned.
    """
    slug = board or kb.DEFAULT_BOARD
    if slug == kb.DEFAULT_BOARD:
        return kb.kanban_home() / "kanban.db"
    return kb.board_dir(slug) / "kanban.db"


def verify_board(board: str, *, limit: int = 10) -> StampCoverage:
    """Return metered-ledger coverage for one Kanban board."""
    db = _db_path_for_board(board)
    if not db.exists():
        return StampCoverage(
            board=board,
            checked_runs=0,
            stamped_runs=0,
            missing_runs=0,
            coverage=1.0,
            required_columns=list(REQUIRED_COLUMNS),
            missing_columns=[],
            sample_missing=[],
        )

    conn = kb.connect(db)
    try:
        cols = _task_run_columns(conn)
        missing_columns = [c for c in REQUIRED_COLUMNS if c not in cols]
        if missing_columns:
            return StampCoverage(
                board=board,
                checked_runs=0,
                stamped_runs=0,
                missing_runs=0,
                coverage=0.0,
                required_columns=list(REQUIRED_COLUMNS),
                missing_columns=missing_columns,
                sample_missing=[],
            )

        select_exprs = []
        for col in (
            "id",
            "task_id",
            "profile",
            "status",
            "outcome",
            "model",
            "tokens_in",
            "tokens_out",
            "cached_tokens",
            "cost_usd",
            "billing_mode",
            "wall_clock_seconds",
            "started_at",
            "ended_at",
        ):
            select_exprs.append(col if col in cols else f"NULL AS {col}")
        status_q = ",".join("?" for _ in _TERMINAL_STATUSES)
        terminal_predicate = f"status IN ({status_q})" if "status" in cols else "0"
        ended_predicate = "ended_at IS NOT NULL" if "ended_at" in cols else "0"
        order_expr = (
            "COALESCE(ended_at, started_at) DESC, id DESC"
            if {"ended_at", "started_at", "id"}.issubset(cols)
            else "id DESC" if "id" in cols else "rowid DESC"
        )
        rows = conn.execute(
            f"""
            SELECT {", ".join(select_exprs)}
              FROM task_runs
             WHERE {terminal_predicate} OR {ended_predicate}
             ORDER BY {order_expr}
            """,
            _TERMINAL_STATUSES,
        ).fetchall()

        sample: list[dict[str, Any]] = []
        stamped = 0
        for row in rows:
            missing = [
                c
                for c in REQUIRED_COLUMNS
                if c != "cached_tokens" and row[c] is None
            ]
            # ``cached_tokens`` can legitimately be zero/unknown, but the column
            # must exist. Treat NULL as unstamped only when every token/cost
            # field is absent so legacy rows surface cleanly below.
            if not missing:
                stamped += 1
                continue
            if len(sample) < limit:
                sample.append(
                    {
                        "run_id": row["id"],
                        "task_id": row["task_id"],
                        "profile": row["profile"],
                        "status": row["status"],
                        "outcome": row["outcome"],
                        "missing": missing,
                    }
                )
        total = len(rows)
        missing_runs = total - stamped
        coverage = 1.0 if total == 0 else stamped / total
        return StampCoverage(
            board=board,
            checked_runs=total,
            stamped_runs=stamped,
            missing_runs=missing_runs,
            coverage=coverage,
            required_columns=list(REQUIRED_COLUMNS),
            missing_columns=[],
            sample_missing=sample,
        )
    finally:
        conn.close()


def verify_all(*, board: str | None = None, limit: int = 10) -> list[StampCoverage]:
    boards = [board] if board else [b["slug"] for b in kb.list_boards(include_archived=False)]
    return [verify_board(slug, limit=limit) for slug in boards]


def _print_text(rows: list[StampCoverage]) -> None:
    for r in rows:
        pct = f"{r.coverage * 100:.1f}%"
        verdict = "OK" if r.ok else "FAIL"
        print(
            f"{verdict} board={r.board} coverage={pct} "
            f"stamped={r.stamped_runs}/{r.checked_runs} missing_runs={r.missing_runs}"
        )
        if r.missing_columns:
            print(f"  missing_columns: {', '.join(r.missing_columns)}")
        for sample in r.sample_missing:
            print(
                "  missing "
                f"run={sample['run_id']} task={sample['task_id']} "
                f"profile={sample.get('profile')} fields={','.join(sample['missing'])}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes stamp")
    sub = parser.add_subparsers(dest="cmd")
    verify = sub.add_parser("verify", help="Verify task_run metered-ledger coverage")
    verify.add_argument("--board", default=None, help="Kanban board slug (default: all active boards)")
    verify.add_argument("--json", action="store_true", help="Emit JSON")
    verify.add_argument("--sample", type=int, default=10, help="Max missing-row samples per board")
    args = parser.parse_args(argv)

    if args.cmd != "verify":
        parser.print_usage()
        return 2

    rows = verify_all(board=args.board, limit=max(0, args.sample))
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=2, sort_keys=True))
    else:
        _print_text(rows)
    return 0 if all(r.ok for r in rows) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
