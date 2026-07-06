"""Tests for the P1 liveness layer (hermes_cli.engine_health + sentinel script).

Covers THE RENDERING RULE (next_beat_due < now == FAILURE regardless of the
row's own status claim), cron zombie detection against a fixture shaped like
the real ~/.hermes/cron/jobs.json, and a smoke run of the out-of-gateway
liveness sentinel script against tmp dirs (no live ~/.hermes reads).
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import engine_health as eh

REPO_ROOT = Path(__file__).resolve().parents[2]
SENTINEL = Path("/home/red/.hermes/scripts/liveness-sentinel.py")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _db_rows(home: Path) -> dict[str, tuple]:
    db = home / "kanban" / "boards" / "engine-health" / "kanban.db"
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            "SELECT component,last_beat,next_beat_due,status,detail FROM cron_heartbeats"
        ).fetchall()
    finally:
        con.close()
    return {r[0]: r for r in rows}


def _iso(offset_seconds: float) -> str:
    """ISO-8601 with utc offset, like the real cron layer writes."""
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=offset_seconds
    )
    return when.isoformat()


def make_job(**overrides) -> dict:
    """A job dict mirroring the real jobs.json field shape."""
    job = {
        "id": "job-x",
        "name": "fixture job",
        "prompt": "do the thing",
        "skills": [],
        "script": None,
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"},
        "enabled": True,
        "state": "scheduled",
        "created_at": "2026-05-28T07:01:21.710900-04:00",
        "next_run_at": _iso(3600),
        "last_run_at": _iso(-3600),
        "last_status": "ok",
        "last_error": None,
        "deliver": "origin",
    }
    job.update(overrides)
    return job


def write_jobs(path: Path, jobs: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"jobs": jobs, "updated_at": _iso(0)}), encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# beat()
# ---------------------------------------------------------------------------

def test_beat_creates_db_and_writes_row(tmp_path):
    assert eh.beat("dispatcher-tick", 120, home=tmp_path) is True
    rows = _db_rows(tmp_path)
    comp, last_beat, due, status, detail = rows["dispatcher-tick"]
    now = int(time.time())
    assert abs(last_beat - now) <= 2
    # grace: next_beat_due = now + interval * 1.5
    assert due == last_beat + int(120 * 1.5)
    assert status == "ok"
    assert detail is None


def test_beat_upserts_single_row_per_component(tmp_path):
    eh.beat("reconciler", 300, home=tmp_path)
    eh.beat("reconciler", 300, status="error: boom", detail="d2", home=tmp_path)
    rows = _db_rows(tmp_path)
    assert len(rows) == 1
    assert rows["reconciler"][3] == "error: boom"
    assert rows["reconciler"][4] == "d2"


def test_beat_respects_hermes_home_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert eh.beat("env-comp", 60) is True
    assert "env-comp" in _db_rows(tmp_path)


def test_beat_never_raises(tmp_path):
    # Point "home" at a regular file so mkdir/connect must fail.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    assert eh.beat("doomed", 60, home=blocker) is False  # no exception


def test_beat_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LIVENESS_BEATS", "0")
    assert eh.beat("switched-off", 60, home=tmp_path) is False
    assert not (tmp_path / "kanban").exists()


# ---------------------------------------------------------------------------
# read_liveness() — THE RENDERING RULE
# ---------------------------------------------------------------------------

def test_fresh_beat_renders_ok(tmp_path):
    eh.beat("fresh", 900, home=tmp_path)
    rows = eh.read_liveness(home=tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["component"] == "fresh"
    assert row["rendered"] == "ok"
    assert row["overdue_seconds"] == 0
    assert row["claimed_status"] == "ok"


def test_overdue_renders_failure_even_when_claiming_ok(tmp_path):
    """The rendering rule: an overdue beat is FAILURE no matter what the
    row's own status field claims. This is the anti-zombie invariant."""
    db = tmp_path / "kanban" / "boards" / "engine-health" / "kanban.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    now = int(time.time())
    con.execute(
        "CREATE TABLE cron_heartbeats (component TEXT PRIMARY KEY,"
        " last_beat INTEGER NOT NULL, next_beat_due INTEGER NOT NULL,"
        " status TEXT, detail TEXT)"
    )
    con.execute(
        "INSERT INTO cron_heartbeats VALUES ('zombie', ?, ?, 'ok', 'I feel great')",
        (now - 7200, now - 3600),
    )
    con.commit()
    con.close()

    rows = eh.read_liveness(home=tmp_path)
    (row,) = rows
    assert row["claimed_status"] == "ok"
    assert row["rendered"] == "FAILURE"
    assert 3595 <= row["overdue_seconds"] <= 3605


def test_read_liveness_missing_db_returns_empty(tmp_path):
    assert eh.read_liveness(home=tmp_path) == []


def test_read_liveness_mixed_rows(tmp_path):
    eh.beat("alive", 900, home=tmp_path)
    # An expired row alongside a fresh one.
    db = tmp_path / "kanban" / "boards" / "engine-health" / "kanban.db"
    con = sqlite3.connect(db)
    now = int(time.time())
    con.execute(
        "INSERT INTO cron_heartbeats VALUES ('dead', ?, ?, 'ok', NULL)",
        (now - 10000, now - 100),
    )
    con.commit()
    con.close()
    rendered = {r["component"]: r["rendered"] for r in eh.read_liveness(home=tmp_path)}
    assert rendered == {"alive": "ok", "dead": "FAILURE"}


# ---------------------------------------------------------------------------
# read_cron_zombies()
# ---------------------------------------------------------------------------

def test_zombie_detection_from_fixture(tmp_path):
    jobs = [
        # Healthy: cron job, next run in the future.
        make_job(id="healthy-cron"),
        # Zombie: interval job (5m), next_run_at 1h in the past (>2x interval),
        # still claiming last_status ok — the classic case.
        make_job(
            id="zombie-interval",
            name="watchdog",
            schedule={"kind": "interval", "minutes": 5, "display": "every 5m"},
            next_run_at=_iso(-3600),
            last_status="ok",
        ),
        # Zombie: cron-expression job >1h past due.
        make_job(id="zombie-cron", next_run_at=_iso(-7200), last_status="ok"),
        # Cron job only 30 min past due: inside the 1h allowance, not a zombie.
        make_job(id="just-late-cron", next_run_at=_iso(-1800)),
        # Interval job past due but under 2x interval: not a zombie.
        make_job(
            id="just-late-interval",
            schedule={"kind": "interval", "minutes": 60, "display": "every 60m"},
            next_run_at=_iso(-3600),
        ),
        # Disabled job, arbitrarily stale: never a zombie.
        make_job(id="disabled-stale", enabled=False, next_run_at=_iso(-864000)),
        # Enabled but nothing scheduled at all: dead in the water.
        make_job(id="never-again", next_run_at=None),
    ]
    path = write_jobs(tmp_path / "cron" / "jobs.json", jobs)

    zombies = eh.read_cron_zombies(jobs_path=path)
    by_id = {z["job_id"]: z for z in zombies}
    assert set(by_id) == {"zombie-interval", "zombie-cron", "never-again"}

    zi = by_id["zombie-interval"]
    assert zi["rendered"] == "FAILURE"
    assert zi["last_status"] == "ok"  # the lie, preserved as evidence
    assert zi["stale_seconds"] >= 3590
    assert zi["threshold_seconds"] == 600  # 2 x 5min

    zc = by_id["zombie-cron"]
    assert zc["threshold_seconds"] == 3600
    assert zc["stale_seconds"] >= 7190

    assert by_id["never-again"]["reason"] == "enabled but no next run scheduled"


def test_zombies_default_path_respects_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_jobs(
        tmp_path / "cron" / "jobs.json",
        [make_job(id="z1", next_run_at=_iso(-7200))],
    )
    zombies = eh.read_cron_zombies()
    assert [z["job_id"] for z in zombies] == ["z1"]


def test_zombies_missing_or_bad_file(tmp_path):
    assert eh.read_cron_zombies(jobs_path=tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert eh.read_cron_zombies(jobs_path=bad) == []


# ---------------------------------------------------------------------------
# liveness-sentinel.py smoke (subprocess, tmp HERMES_HOME, no live reads)
# ---------------------------------------------------------------------------

def _run_sentinel(home: Path, repo: str | Path = REPO_ROOT, *, no_site: bool = False):
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["HERMES_AGENT_REPO"] = str(repo)
    env.pop("HERMES_LIVENESS_BEATS", None)
    # -S skips site-packages: simulates a broken venv (hermes_cli was pip
    # installed, so a bogus repo path alone doesn't break the import).
    argv = [sys.executable] + (["-S"] if no_site else []) + [str(SENTINEL)]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.mark.skipif(not SENTINEL.exists(), reason="sentinel script not installed")
def test_sentinel_reports_failures_and_beats(tmp_path):
    write_jobs(
        tmp_path / "cron" / "jobs.json",
        [
            make_job(
                id="zombie-1",
                name="dead watchdog",
                schedule={"kind": "interval", "minutes": 5, "display": "every 5m"},
                next_run_at=_iso(-7200),
                last_status="ok",
            ),
            make_job(id="fine-1"),
        ],
    )
    # Pre-seed an expired heartbeat claiming ok — must render FAILURE.
    eh.beat("old-component", 60, home=tmp_path)
    db = tmp_path / "kanban" / "boards" / "engine-health" / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE cron_heartbeats SET next_beat_due = ? WHERE component='old-component'",
        (int(time.time()) - 5000,),
    )
    con.commit()
    con.close()

    proc = _run_sentinel(tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "CRON ZOMBIE: job=zombie-1" in out
    assert "last_status='ok'" in out
    assert "LIVENESS FAILURE: component=old-component" in out
    assert "fine-1" not in out

    # Its own beat was written regardless.
    rows = _db_rows(tmp_path)
    assert "liveness-sentinel" in rows
    assert abs(rows["liveness-sentinel"][1] - int(time.time())) <= 30


@pytest.mark.skipif(not SENTINEL.exists(), reason="sentinel script not installed")
def test_sentinel_silent_when_healthy(tmp_path):
    write_jobs(tmp_path / "cron" / "jobs.json", [make_job(id="fine-1")])
    proc = _run_sentinel(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""  # empty stdout = no cron delivery, by design
    assert "liveness-sentinel" in _db_rows(tmp_path)


@pytest.mark.skipif(not SENTINEL.exists(), reason="sentinel script not installed")
def test_sentinel_fallback_mode_without_repo(tmp_path):
    """The sentinel's raison d'etre: still works when hermes_cli is broken."""
    write_jobs(
        tmp_path / "cron" / "jobs.json",
        [make_job(id="zombie-fb", next_run_at=_iso(-7200), last_status="ok")],
    )
    proc = _run_sentinel(tmp_path, repo=tmp_path / "no-such-repo", no_site=True)
    assert proc.returncode == 0, proc.stderr
    assert "CRON ZOMBIE: job=zombie-fb" in proc.stdout
    rows = _db_rows(tmp_path)
    assert "liveness-sentinel" in rows
    assert "mode=fallback" in (rows["liveness-sentinel"][4] or "")


@pytest.mark.skipif(not SENTINEL.exists(), reason="sentinel script not installed")
def test_sentinel_webhook_failure_is_silent(tmp_path):
    """A webhook file pointing at a dead endpoint must not crash the run."""
    write_jobs(
        tmp_path / "cron" / "jobs.json",
        [make_job(id="zombie-wh", next_run_at=_iso(-7200))],
    )
    (tmp_path / "discord-alert-webhook").write_text("http://127.0.0.1:1/dead\n")
    proc = _run_sentinel(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "CRON ZOMBIE: job=zombie-wh" in proc.stdout
