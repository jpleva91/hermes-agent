"""Engine liveness heartbeat ledger (Mission Engine rework program — P1 LIVENESS LAYER).

The research behind the rework found 13+/35 cron jobs zombied while still
claiming ``last: ok``, a dispatch watchdog dead for 8 days, and a digest
silent for 6+ days — every one of them *self-reporting healthy*.  The fix is
structural: components no longer get to assert their own liveness.  They
write heartbeats into the ``cron_heartbeats`` table on the ``engine-health``
board DB, and every reader applies THE RENDERING RULE:

    ``next_beat_due < now  ==  FAILURE`` — regardless of the row's own
    ``status`` claim.  A component's opinion of itself is never trusted
    over the clock.

Three entry points:

* :func:`beat` — UPSERT a heartbeat.  Never raises (a liveness layer that
  can crash its host is worse than no liveness layer).  Creates the DB and
  table if missing; does **not** depend on ``kanban_db``.
* :func:`read_liveness` — every heartbeat row rendered through the rule.
* :func:`read_cron_zombies` — parses ``~/.hermes/cron/jobs.json`` and flags
  enabled jobs whose ``next_run_at`` is stale: past due by more than 2x the
  interval for interval jobs, or by more than 1 hour for cron-expression
  jobs.  This is the layer that catches zombies still reading ``ok``.

Env:

* ``HERMES_HOME`` — overrides the ``~/.hermes`` root (tests, containers).
* ``HERMES_LIVENESS_BEATS=0`` — kill-switch: :func:`beat` becomes a no-op.

Schema (contract copy — the orchestrator creates the same table via
``kanban_db``; ``CREATE TABLE IF NOT EXISTS`` keeps both sides compatible)::

    CREATE TABLE cron_heartbeats (
      component TEXT PRIMARY KEY,
      last_beat INTEGER NOT NULL,
      next_beat_due INTEGER NOT NULL,
      status TEXT,
      detail TEXT
    );
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Union

_log = logging.getLogger(__name__)

#: Slug of the board whose DB carries the heartbeat ledger.
ENGINE_HEALTH_BOARD = "engine-health"

#: ``next_beat_due = now + interval * GRACE_FACTOR`` — beats get 50% slack
#: before the rendering rule declares them dead.
GRACE_FACTOR = 1.5

#: Interval-scheduled cron jobs are zombies when overdue by more than
#: ``CRON_INTERVAL_ZOMBIE_FACTOR * interval``.
CRON_INTERVAL_ZOMBIE_FACTOR = 2.0

#: Cron-expression jobs are zombies when ``next_run_at`` is more than this
#: many seconds in the past (they should have been rescheduled by then).
CRON_EXPR_STALE_SECONDS = 3600

#: Kill-switch env var — truthy-off values make :func:`beat` a no-op.
LIVENESS_BEATS_ENV = "HERMES_LIVENESS_BEATS"

_OFF_VALUES = {"0", "false", "off", "no"}

_HEARTBEAT_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS cron_heartbeats ("
    " component TEXT PRIMARY KEY,"
    " last_beat INTEGER NOT NULL,"
    " next_beat_due INTEGER NOT NULL,"
    " status TEXT,"
    " detail TEXT"
    ")"
)

#: Single-statement UPSERT per the shared conventions in the build contract.
_UPSERT_SQL = (
    "INSERT INTO cron_heartbeats(component,last_beat,next_beat_due,status,detail)"
    " VALUES(?,?,?,?,?)"
    " ON CONFLICT(component) DO UPDATE SET"
    " last_beat=excluded.last_beat,"
    " next_beat_due=excluded.next_beat_due,"
    " status=excluded.status,"
    " detail=excluded.detail"
)


def resolve_home(home: Optional[Union[str, Path]] = None) -> Path:
    """Resolve the Hermes home dir: explicit arg > ``HERMES_HOME`` > ``~/.hermes``."""
    if home is not None:
        return Path(home)
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".hermes"


def heartbeat_db_path(home: Optional[Union[str, Path]] = None) -> Path:
    """Path of the engine-health board DB that carries ``cron_heartbeats``."""
    return resolve_home(home) / "kanban" / "boards" / ENGINE_HEALTH_BOARD / "kanban.db"


def beat(
    component: str,
    interval_seconds: int,
    status: str = "ok",
    detail: Optional[str] = None,
    home: Optional[Union[str, Path]] = None,
) -> bool:
    """Record a heartbeat for ``component``.  NEVER raises.

    ``next_beat_due`` is set to ``now + interval_seconds * 1.5`` (grace).
    Creates the engine-health DB and ``cron_heartbeats`` table if missing —
    heartbeat writers must not depend on the kanban layer being importable.

    Returns ``True`` when the row was written, ``False`` on any failure or
    when the ``HERMES_LIVENESS_BEATS=0`` kill-switch is set.  Callers should
    treat the return value as advisory only; the whole point is that a
    broken liveness write must never take its host component down with it.
    """
    try:
        if os.environ.get(LIVENESS_BEATS_ENV, "").strip().lower() in _OFF_VALUES:
            return False
        now = int(time.time())
        due = now + int(int(interval_seconds) * GRACE_FACTOR)
        db_path = heartbeat_db_path(home)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path), timeout=5)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(_HEARTBEAT_TABLE_SQL)
            con.execute(_UPSERT_SQL, (str(component), now, due, status, detail))
            con.commit()
        finally:
            con.close()
        return True
    except Exception as exc:  # noqa: BLE001 — contract: beat() never raises.
        try:
            _log.warning("engine_health.beat(%r) failed: %s", component, exc)
        except Exception:
            pass
        return False


def read_liveness(
    home: Optional[Union[str, Path]] = None,
    *,
    now: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Every heartbeat row, rendered through THE RENDERING RULE.

    ``next_beat_due < now`` renders ``FAILURE`` regardless of the row's own
    ``status`` claim.  Returns a list of::

        {component, last_beat, next_beat_due, claimed_status,
         rendered: 'ok'|'FAILURE', overdue_seconds}

    Missing DB / missing table -> ``[]`` (nothing has ever beaten; the cron
    zombie layer and the dead-man watcher cover the "ledger never appeared"
    case).
    """
    ts_now = int(time.time()) if now is None else int(now)
    db_path = heartbeat_db_path(home)
    if not db_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(str(db_path), timeout=5)
        try:
            cur = con.execute(
                "SELECT component, last_beat, next_beat_due, status, detail"
                " FROM cron_heartbeats ORDER BY component"
            )
            fetched = cur.fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        _log.warning("engine_health.read_liveness failed: %s", exc)
        return []
    for component, last_beat, next_beat_due, claimed_status, detail in fetched:
        overdue = max(0, ts_now - int(next_beat_due))
        rows.append(
            {
                "component": component,
                "last_beat": int(last_beat),
                "next_beat_due": int(next_beat_due),
                "claimed_status": claimed_status,
                # THE RENDERING RULE — the row's own claim is never consulted.
                "rendered": "FAILURE" if int(next_beat_due) < ts_now else "ok",
                "overdue_seconds": overdue,
                "detail": detail,
            }
        )
    return rows


def _parse_when(value: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp (the ``next_run_at`` format used by the
    Hermes cron layer, e.g. ``2026-07-06T09:00:00-04:00``) to a Unix ts."""
    try:
        parsed = _dt.datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        # Naive timestamps are written in host-local time by the cron layer.
        parsed = parsed.astimezone()
    return parsed.timestamp()


def read_cron_zombies(
    jobs_path: Optional[Union[str, Path]] = None,
    *,
    home: Optional[Union[str, Path]] = None,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Flag enabled Hermes cron jobs whose schedule has silently died.

    Reads ``<home>/cron/jobs.json`` (shape: ``{"jobs": [...]}``; per-job
    fields ``enabled``, ``next_run_at``, ``schedule.kind`` in
    ``{'interval','cron'}``, ``schedule.minutes``, ``last_status``).

    A job renders FAILURE when it is enabled and:

    * ``next_run_at`` is in the past by more than 2x its interval
      (interval jobs), or by more than 1h (cron-expression jobs); or
    * it has no ``next_run_at``/``next_run`` at all while claiming to be
      scheduled (nothing will ever fire it again).

    The ``last_status`` claim is reported alongside — these are exactly the
    zombies that read ``ok`` while dead.  Never raises; unreadable file or
    malformed entries degrade to skipping (the sentinel's fallback path and
    the dead-man watcher backstop total losses).
    """
    ts_now = time.time() if now is None else float(now)
    path = Path(jobs_path) if jobs_path is not None else resolve_home(home) / "cron" / "jobs.json"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("engine_health.read_cron_zombies: cannot read %s: %s", path, exc)
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    zombies: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled"):
            continue
        schedule = job.get("schedule") or {}
        kind = schedule.get("kind") or "cron"
        next_run_raw = job.get("next_run_at") or job.get("next_run")
        base = {
            "job_id": job.get("id"),
            "name": job.get("name"),
            "kind": kind,
            "rendered": "FAILURE",
            "last_status": job.get("last_status"),
            "state": job.get("state"),
            "next_run_at": next_run_raw,
        }
        if not next_run_raw:
            # Enabled but nothing scheduled: unless it is mid-run, it's dead.
            if job.get("state") not in ("running",):
                zombies.append(
                    {
                        **base,
                        "stale_seconds": None,
                        "threshold_seconds": None,
                        "reason": "enabled but no next run scheduled",
                    }
                )
            continue
        next_run_ts = _parse_when(next_run_raw)
        if next_run_ts is None:
            zombies.append(
                {
                    **base,
                    "stale_seconds": None,
                    "threshold_seconds": None,
                    "reason": f"unparseable next_run_at: {next_run_raw!r}",
                }
            )
            continue
        if kind == "interval":
            minutes = schedule.get("minutes")
            try:
                interval_s = max(60.0, float(minutes) * 60.0)
            except (TypeError, ValueError):
                interval_s = float(CRON_EXPR_STALE_SECONDS)
            threshold = CRON_INTERVAL_ZOMBIE_FACTOR * interval_s
        else:
            threshold = float(CRON_EXPR_STALE_SECONDS)
        stale = ts_now - next_run_ts
        if stale > threshold:
            zombies.append(
                {
                    **base,
                    "stale_seconds": int(stale),
                    "threshold_seconds": int(threshold),
                    "reason": (
                        f"next run {int(stale)}s in the past"
                        f" (> {int(threshold)}s threshold), last_status claims"
                        f" {job.get('last_status')!r}"
                    ),
                }
            )
    return zombies
