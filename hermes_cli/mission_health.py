"""Mission Engine single-pane health report (rework program P1).

``hermes mission-health`` (and ``python -m hermes_cli.mission_health``) renders
ONE terminal pane answering "is the engine alive and honest right now?" by
assembling seven best-effort sections:

1. component liveness  — cron_heartbeats + cron-job zombie scan.  The
   RENDERING RULE from the rework contract is enforced here and cannot be
   overridden by any component's self-reported status:
   ``next_beat_due < now  ==  FAILURE`` regardless of anything else.
2. board stalls        — blocked cards >12h, dead-zone todos (>4h old with
   every parent done — the class the dispatcher silently forgot), and cards
   whose ``wake_deadline`` already passed.
3. backup freshness    — newest kanban backup must be <6h old.
4. evidence survival   — sample newest mission-artifact manifests, verify the
   tarballs still exist (and spot-check sha256) — % of evidence surviving.
5. cost (24h)          — token/cost sums from task_runs plus model coverage.
6. overhead ratio      — fraction of live cards that are gate/cure/routing/
   hygiene mechanics; alarm above the 15% SLO (research: 67.1% on fable).
7. verdict coverage    — task_verdicts rows vs gatewarden-ish completions.

Design contract (REWORK-SPEC P1):

* Every section is fail-isolated: a crashing section renders as its own
  FAILURE line; the pane itself never crashes.
* Silence is signal: a healthy engine renders each section as ONE green
  line (well under 60 lines total).  Only failures grow detail lines.
* Exit code 0 == all green, 1 == any FAILURE, so cron/scripts can gate on it.
* ``hermes_cli.engine_health`` (built by a sibling agent against the same
  contract) is imported lazily; when absent or unusable this module degrades
  to reading the engine-health board's ``cron_heartbeats`` table and
  ``~/.hermes/cron/jobs.json`` directly.
* All board DB access is read-only (sqlite URI ``mode=ro``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

_log = logging.getLogger(__name__)

# ── thresholds (REWORK-SPEC research numbers — do not re-derive) ────────────
BLOCKED_STALL_SECONDS = 12 * 3600      #: blocked card older than this == stall
DEADZONE_TODO_SECONDS = 4 * 3600       #: ready todo older than this == dead zone
BACKUP_MAX_AGE_SECONDS = 21600         #: 6h — newest kanban backup must beat this
OVERHEAD_ALARM_RATIO = 0.15            #: governance-overhead SLO alarm threshold
EVIDENCE_SAMPLE = 50                   #: newest manifests sampled
EVIDENCE_HASH_SAMPLE = 10              #: of those, tarballs fully sha256-verified
CRON_ZOMBIE_GRACE_SECONDS = 900        #: next_run_at this far past == zombie
DETAIL_ID_CAP = 4                      #: card ids listed per stall class
WINDOW_24H = 86400

#: Governance-mechanics title pattern (overhead ratio + gatewarden-ish match).
OVERHEAD_RE = re.compile(r"(?i)\b(gate|cure|re-?gate|routing|hygiene|handoff)\b")

SECTION_ORDER = (
    "liveness", "stalls", "backups", "evidence", "cost", "overhead", "verdicts",
)

_TERMINAL_STATUSES = ("done", "archived")


# ── small helpers ────────────────────────────────────────────────────────────

def _resolve_home(home: Optional[Path | str]) -> Path:
    if home is not None:
        return Path(home).expanduser()
    env = (os.environ.get("HERMES_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def _list_boards(home: Path) -> list[tuple[str, Path]]:
    root = home / "kanban" / "boards"
    if not root.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for d in sorted(root.iterdir()):
        db = d / "kanban.db"
        if db.is_file():
            out.append((d.name, db))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 60}m"


def _fmt_tokens(n: float) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(n)


def _cap_ids(ids: list[str]) -> str:
    shown = " ".join(ids[:DETAIL_ID_CAP])
    extra = len(ids) - DETAIL_ID_CAP
    return f"[{shown}{f' +{extra} more' if extra > 0 else ''}]"


def _parse_iso_ts(raw: Any) -> Optional[float]:
    """Best-effort ISO-8601 → epoch seconds (jobs.json timestamps)."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _section_guard(name: str, fn: Callable[[], dict]) -> dict:
    """Fail-isolation boundary: a crashing section becomes its own FAILURE."""
    try:
        sec = fn()
    except Exception as exc:  # noqa: BLE001 — the pane must never crash
        _log.warning("mission-health: section %s crashed: %s", name, exc)
        sec = {"status": "FAIL", "summary": f"section crashed: {exc}", "detail": []}
    sec.setdefault("detail", [])
    return sec


# ── section 1: component liveness ────────────────────────────────────────────

def _call_flexible(fn: Callable, home: Path) -> Any:
    """Call a sibling-contract function tolerating unknown signatures."""
    for attempt in (lambda: fn(home=home), lambda: fn(home), lambda: fn()):
        try:
            return attempt()
        except TypeError:
            continue
    return None


def _normalize_heartbeats(obj: Any) -> Optional[list[dict]]:
    """Coerce engine_health.read_liveness output into cron_heartbeats rows."""
    if isinstance(obj, dict) and isinstance(obj.get("components"), (list, dict)):
        obj = obj["components"]
    if isinstance(obj, dict):
        rows = []
        for name, info in obj.items():
            if not isinstance(info, dict):
                return None
            rows.append({"component": str(name), **info})
        obj = rows
    if not isinstance(obj, list):
        return None
    out = []
    for item in obj:
        if not isinstance(item, dict):
            return None
        out.append({
            "component": str(item.get("component") or item.get("name") or "?"),
            "last_beat": item.get("last_beat"),
            "next_beat_due": item.get("next_beat_due"),
            "status": item.get("status"),
        })
    return out


def _read_heartbeats_direct(home: Path) -> Optional[list[dict]]:
    """Fallback: read cron_heartbeats straight off the engine-health board DB."""
    db = home / "kanban" / "boards" / "engine-health" / "kanban.db"
    if not db.is_file():
        return None
    try:
        con = _connect_ro(db)
        try:
            rows = con.execute(
                "SELECT component, last_beat, next_beat_due, status FROM cron_heartbeats"
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as exc:
        _log.warning("mission-health: heartbeat read failed: %s", exc)
        return None


def _read_cron_zombies_direct(home: Path, now: int) -> Optional[tuple[list[str], int]]:
    """Fallback zombie scan: enabled jobs whose next_run_at is already past."""
    jobs_path = home / "cron" / "jobs.json"
    if not jobs_path.is_file():
        return None
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None
    zombies: list[str] = []
    total = 0
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled"):
            continue
        total += 1
        nxt = _parse_iso_ts(job.get("next_run_at"))
        if nxt is not None and nxt < now - CRON_ZOMBIE_GRACE_SECONDS:
            zombies.append(str(job.get("id") or job.get("name") or "?"))
    return zombies, total


def _gather_liveness(home: Path, now: int) -> dict:
    try:
        import importlib

        eh: Any = importlib.import_module("hermes_cli.engine_health")
    except Exception:  # module not built yet — sibling agent owns it
        eh = None

    detail: list[str] = []
    failures = 0

    # -- heartbeats ----------------------------------------------------------
    beats: Optional[list[dict]] = None
    source = "engine_health"
    if eh is not None and callable(getattr(eh, "read_liveness", None)):
        try:
            beats = _normalize_heartbeats(_call_flexible(eh.read_liveness, home))
        except Exception as exc:  # noqa: BLE001
            _log.warning("mission-health: engine_health.read_liveness failed: %s", exc)
    if beats is None:
        beats = _read_heartbeats_direct(home)
        source = "cron_heartbeats"

    beat_total = len(beats) if beats else 0
    beat_ok = 0
    if not beats:
        failures += 1
        detail.append(
            "no heartbeat data (engine-health board / cron_heartbeats missing)"
        )
    else:
        for row in beats:
            comp = row.get("component", "?")
            due = row.get("next_beat_due")
            claim = str(row.get("status") or "").strip()
            # RENDERING RULE: next_beat_due < now == FAILURE regardless of the
            # component's own status claim — the claim is NEVER trusted.
            if isinstance(due, (int, float)) and due < now:
                failures += 1
                detail.append(
                    f"{comp}: beat overdue {_fmt_age(now - due)}"
                    f" (claims {claim or 'nothing'!r})"
                )
            elif claim.lower().startswith("error"):
                failures += 1
                detail.append(f"{comp}: self-reports {claim!r}")
            else:
                beat_ok += 1

    # -- cron zombies ---------------------------------------------------------
    zombies: Optional[list[str]] = None
    job_total: Optional[int] = None
    if eh is not None and callable(getattr(eh, "read_cron_zombies", None)):
        try:
            raw = _call_flexible(eh.read_cron_zombies, home)
            if isinstance(raw, (list, tuple)):
                zombies = [
                    str(z.get("id") or z.get("name") or "?") if isinstance(z, dict) else str(z)
                    for z in raw
                ]
        except Exception as exc:  # noqa: BLE001
            _log.warning("mission-health: engine_health.read_cron_zombies failed: %s", exc)
    if zombies is None:
        try:
            direct = _read_cron_zombies_direct(home, now)
        except Exception as exc:  # noqa: BLE001
            direct = None
            failures += 1
            detail.append(f"cron jobs.json unreadable: {exc}")
        if direct is not None:
            zombies, job_total = direct

    if zombies:
        failures += 1
        detail.append(f"cron zombies: {_cap_ids(sorted(zombies))}")

    zombie_txt = (
        f"{len(zombies)}{f'/{job_total}' if job_total is not None else ''} cron zombies"
        if zombies is not None
        else "cron jobs unknown"
    )
    summary = f"{beat_ok}/{beat_total} components beating ({source}); {zombie_txt}"
    return {
        "status": "FAIL" if failures else "OK",
        "summary": summary,
        "detail": detail,
        "heartbeats": beats or [],
        "zombies": zombies or [],
    }


# ── section 2: board stalls ──────────────────────────────────────────────────

_DEADZONE_SQL = """
SELECT t.id FROM tasks t
WHERE t.status = 'todo' AND t.created_at <= ?
  AND NOT EXISTS (
    SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id
    WHERE l.child_id = t.id AND p.status NOT IN ('done', 'archived')
  )
ORDER BY t.created_at
"""


def _gather_stalls(home: Path, now: int) -> dict:
    boards = _list_boards(home)
    if not boards:
        return {"status": "FAIL", "summary": "no boards found under kanban/boards",
                "detail": []}
    detail: list[str] = []
    notes: list[str] = []
    stall_total = 0
    bad_boards = 0
    for slug, db in boards:
        try:
            con = _connect_ro(db)
        except sqlite3.Error as exc:
            bad_boards += 1
            detail.append(f"{slug}: unreadable ({exc})")
            continue
        try:
            parts: list[str] = []
            blocked = [r["id"] for r in con.execute(
                "SELECT id FROM tasks WHERE status='blocked' AND created_at <= ?"
                " ORDER BY created_at", (now - BLOCKED_STALL_SECONDS,))]
            try:
                dead = [r["id"] for r in con.execute(
                    _DEADZONE_SQL, (now - DEADZONE_TODO_SECONDS,))]
            except sqlite3.OperationalError:  # no task_links table
                dead = [r["id"] for r in con.execute(
                    "SELECT id FROM tasks WHERE status='todo' AND created_at <= ?"
                    " ORDER BY created_at", (now - DEADZONE_TODO_SECONDS,))]
            try:
                awol = [r["id"] for r in con.execute(
                    "SELECT id FROM tasks WHERE wake_deadline IS NOT NULL"
                    " AND wake_deadline < ? AND status NOT IN (?, ?)",
                    (now, *_TERMINAL_STATUSES))]
            except sqlite3.OperationalError:  # wake_deadline not deployed yet
                awol = []
            if blocked:
                parts.append(f"{len(blocked)} blocked>12h {_cap_ids(blocked)}")
            if dead:
                parts.append(f"{len(dead)} dead-zone todo {_cap_ids(dead)}")
            if awol:
                parts.append(f"{len(awol)} wake-overdue {_cap_ids(awol)}")
            if parts:
                stall_total += len(blocked) + len(dead) + len(awol)
                bad_boards += 1
                detail.append(f"{slug}: " + "; ".join(parts))
        except sqlite3.Error as exc:
            if "no such table" in str(exc).lower():
                notes.append(f"{slug}: no tasks table (skipped)")
            else:
                bad_boards += 1
                detail.append(f"{slug}: query failed ({exc})")
        finally:
            con.close()
    ok = bad_boards == 0
    summary = (
        f"no stalls across {len(boards)} boards"
        if ok else f"{stall_total} stalled cards on {bad_boards}/{len(boards)} boards"
    )
    return {"status": "OK" if ok else "FAIL", "summary": summary,
            "detail": (detail + notes) if not ok else []}


# ── section 3: backup freshness ──────────────────────────────────────────────

def _gather_backups(home: Path, now: int) -> dict:
    root = home / "hermes-runtime" / "backups" / "kanban"
    newest: Optional[float] = None
    if root.is_dir():
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    mt = p.stat().st_mtime
                    if newest is None or mt > newest:
                        newest = mt
            except OSError:
                continue
    if newest is None:
        return {"status": "FAIL",
                "summary": f"no kanban backups found under {root}", "detail": []}
    age = now - newest
    if age > BACKUP_MAX_AGE_SECONDS:
        return {"status": "FAIL",
                "summary": f"newest backup {_fmt_age(age)} old (limit "
                           f"{_fmt_age(BACKUP_MAX_AGE_SECONDS)})",
                "detail": []}
    return {"status": "OK",
            "summary": f"newest backup {_fmt_age(age)} old (limit "
                       f"{_fmt_age(BACKUP_MAX_AGE_SECONDS)})",
            "detail": []}


# ── section 4: evidence survival ─────────────────────────────────────────────

def _gather_evidence(home: Path, now: int) -> dict:
    from hermes_cli.mission_artifacts import TARBALL_NAME, default_archive_root

    root = default_archive_root(home)
    manifests: list[Path] = []
    if root.is_dir():
        manifests = list(root.rglob("manifest.json"))

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    manifests.sort(key=_mtime, reverse=True)
    manifests = manifests[:EVIDENCE_SAMPLE]
    if not manifests:
        return {"status": "OK", "summary": "no mission-artifact manifests yet",
                "detail": []}

    surviving = 0
    hashed = 0
    casualties: list[str] = []
    for mpath in manifests:
        reason: Optional[str] = None
        try:
            man = json.loads(mpath.read_text(encoding="utf-8"))
            tar = man.get("tarball")
            if tar:
                tpath = mpath.parent / str(tar.get("name") or TARBALL_NAME)
                if not tpath.is_file():
                    reason = "tarball missing"
                elif hashed < EVIDENCE_HASH_SAMPLE:
                    hashed += 1
                    if _sha256(tpath) != tar.get("sha256"):
                        reason = "sha256 mismatch"
        except Exception as exc:  # noqa: BLE001 — one bad manifest != pane crash
            reason = f"manifest unreadable ({exc})"
        if reason is None:
            surviving += 1
        else:
            casualties.append(f"{mpath.parent.relative_to(root)}: {reason}")
    pct = round(100.0 * surviving / len(manifests))
    summary = (f"{pct}% evidence surviving ({surviving}/{len(manifests)} manifests,"
               f" {hashed} sha256-verified)")
    return {"status": "OK" if surviving == len(manifests) else "FAIL",
            "summary": summary, "detail": casualties[:6], "percent": pct}


# ── section 5: cost 24h ──────────────────────────────────────────────────────

def _gather_cost(home: Path, now: int) -> dict:
    boards = _list_boards(home)
    cutoff = now - WINDOW_24H
    tok_in = tok_out = with_model = runs = 0
    cost = 0.0
    untelemetered = 0
    for slug, db in boards:
        try:
            con = _connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            try:
                row = con.execute(
                    "SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),"
                    " COALESCE(SUM(cost_usd),0.0),"
                    " COALESCE(SUM(CASE WHEN model IS NOT NULL AND model != ''"
                    " THEN 1 ELSE 0 END),0), COUNT(*)"
                    " FROM task_runs WHERE started_at >= ?", (cutoff,)).fetchone()
                tok_in += row[0]
                tok_out += row[1]
                cost += row[2]
                with_model += row[3]
                runs += row[4]
            except sqlite3.OperationalError:
                # cost columns / task_runs not deployed on this board yet
                try:
                    n = con.execute(
                        "SELECT COUNT(*) FROM task_runs WHERE started_at >= ?",
                        (cutoff,)).fetchone()[0]
                    runs += n
                    untelemetered += n
                except sqlite3.OperationalError:
                    pass
        except sqlite3.Error as exc:
            _log.warning("mission-health: cost scan failed on %s: %s", slug, exc)
        finally:
            con.close()
    coverage = round(100.0 * with_model / runs) if runs else 100
    summary = (f"{runs} runs, {_fmt_tokens(tok_in)} in / {_fmt_tokens(tok_out)} out,"
               f" ${cost:.2f}, model coverage {coverage}% ({with_model}/{runs})")
    if untelemetered:
        summary += f" [{untelemetered} runs on boards without cost columns]"
    return {"status": "OK", "summary": summary, "detail": [],
            "tokens_in": tok_in, "tokens_out": tok_out, "cost_usd": cost,
            "coverage_pct": coverage}


# ── section 6: overhead ratio ────────────────────────────────────────────────

def _gather_overhead(home: Path, now: int) -> dict:
    boards = _list_boards(home)
    detail: list[str] = []
    alarms = 0
    measured = 0
    for slug, db in boards:
        try:
            con = _connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            titles = [r[0] or "" for r in con.execute(
                "SELECT title FROM tasks WHERE status != 'archived'")]
        except sqlite3.Error:
            continue
        finally:
            con.close()
        if not titles:
            continue
        measured += 1
        hits = sum(1 for t in titles if OVERHEAD_RE.search(t))
        ratio = hits / len(titles)
        if ratio > OVERHEAD_ALARM_RATIO:
            alarms += 1
            detail.append(
                f"{slug}: {round(100 * ratio)}% overhead ({hits}/{len(titles)} cards)"
            )
    summary = (
        f"all {measured} boards <= {round(100 * OVERHEAD_ALARM_RATIO)}%"
        " governance overhead"
        if not alarms
        else f"{alarms}/{measured} boards above {round(100 * OVERHEAD_ALARM_RATIO)}%"
             " overhead SLO"
    )
    return {"status": "OK" if not alarms else "FAIL", "summary": summary,
            "detail": detail}


# ── section 7: verdict coverage ──────────────────────────────────────────────

_GATEWARDENISH_SQL = """
SELECT COUNT(*) FROM task_runs
WHERE COALESCE(ended_at, started_at) >= ? AND outcome = 'completed'
  AND (lower(COALESCE(profile,'')) LIKE '%warden%'
    OR lower(COALESCE(profile,'')) LIKE '%review%'
    OR lower(COALESCE(profile,'')) LIKE '%judge%'
    OR lower(COALESCE(profile,'')) LIKE '%gate%')
"""


def _gather_verdicts(home: Path, now: int) -> dict:
    boards = _list_boards(home)
    cutoff = now - WINDOW_24H
    verdicts = 0
    completions = 0
    table_seen = False
    for slug, db in boards:
        try:
            con = _connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            try:
                verdicts += con.execute(
                    "SELECT COUNT(*) FROM task_verdicts WHERE created_at >= ?",
                    (cutoff,)).fetchone()[0]
                table_seen = True
            except sqlite3.OperationalError:
                pass  # task_verdicts not deployed on this board yet
            try:
                completions += con.execute(_GATEWARDENISH_SQL, (cutoff,)).fetchone()[0]
            except sqlite3.OperationalError:
                pass
        except sqlite3.Error as exc:
            _log.warning("mission-health: verdict scan failed on %s: %s", slug, exc)
        finally:
            con.close()
    if not table_seen:
        return {"status": "OK",
                "summary": f"task_verdicts not deployed yet"
                           f" ({completions} gatewarden-ish completions 24h)",
                "detail": []}
    # Reviews completing without recorded verdicts is exactly the invisible-
    # judgement failure the rework exists to kill — 0 coverage with real
    # completions is a FAILURE, partial coverage renders but stays green.
    fail = completions > 0 and verdicts == 0
    summary = f"{verdicts} verdicts / {completions} gatewarden-ish completions (24h)"
    return {"status": "FAIL" if fail else "OK", "summary": summary, "detail": []}


# ── assembly + rendering ─────────────────────────────────────────────────────

def gather(home: Optional[Path | str] = None, *, now: Optional[int] = None) -> dict:
    """Assemble the mission-health pane data (best-effort per section)."""
    home_path = _resolve_home(home)
    ts = int(now if now is not None else time.time())
    builders: dict[str, Callable[[], dict]] = {
        "liveness": lambda: _gather_liveness(home_path, ts),
        "stalls": lambda: _gather_stalls(home_path, ts),
        "backups": lambda: _gather_backups(home_path, ts),
        "evidence": lambda: _gather_evidence(home_path, ts),
        "cost": lambda: _gather_cost(home_path, ts),
        "overhead": lambda: _gather_overhead(home_path, ts),
        "verdicts": lambda: _gather_verdicts(home_path, ts),
    }
    sections = {name: _section_guard(name, fn) for name, fn in builders.items()}
    return {
        "generated_at": ts,
        "home": str(home_path),
        "sections": sections,
        "ok": all(s.get("status") == "OK" for s in sections.values()),
    }


def render(data: dict) -> str:
    """Render the pane: one line per green section, detail only on failure."""
    when = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(data["generated_at"]))
    lines = [f"MISSION HEALTH  {when}  home={data['home']}"]
    failing = 0
    for name in SECTION_ORDER:
        sec = data["sections"].get(name)
        if sec is None:
            sec = {"status": "FAIL", "summary": "section missing", "detail": []}
        status = "OK" if sec.get("status") == "OK" else "FAIL"
        if status == "FAIL":
            failing += 1
        lines.append(f" {status:<4} {name:<9} {sec.get('summary', '')}")
        for d in sec.get("detail", []):
            lines.append(f"        {d}")
    verdict = "OK — all sections green" if failing == 0 else \
        f"FAIL — {failing} failing section(s)"
    lines.append(f"OVERALL: {verdict}")
    return "\n".join(lines)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI entry: print the pane; exit 0 all green / 1 any FAILURE."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="hermes mission-health",
        description="Mission Engine single-pane health report (rework P1).",
    )
    parser.add_argument("--home", default=None,
                        help="Hermes home (default: $HERMES_HOME or ~/.hermes)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    data = gather(args.home)
    print(render(data))
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
