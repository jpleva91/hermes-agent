"""Tests for hermes_cli.mission_health (rework program P1 single pane).

All fixtures are fabricated under tmp_path — no live ~/.hermes reads.  Board
DBs carry only the columns mission_health actually queries (schema mirrored
from the real kanban.db inspected read-only per the build contract).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tarfile
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import mission_health

NOW = int(time.time())
HOUR = 3600


# ── fixture builders ─────────────────────────────────────────────────────────

def _mk_board(home: Path, slug: str, *, wake_col: bool = True,
              links: bool = True, cost_cols: bool = True,
              verdicts: bool = False) -> Path:
    d = home / "kanban" / "boards" / slug
    d.mkdir(parents=True)
    db = d / "kanban.db"
    con = sqlite3.connect(db)
    cols = ("id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,"
            " created_at INTEGER NOT NULL, completed_at INTEGER,"
            " workspace_path TEXT")
    if wake_col:
        cols += ", wake_deadline INTEGER"
    con.execute(f"CREATE TABLE tasks ({cols})")
    if links:
        con.execute("CREATE TABLE task_links (parent_id TEXT NOT NULL,"
                    " child_id TEXT NOT NULL, PRIMARY KEY (parent_id, child_id))")
    run_cols = ("id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
                " profile TEXT, status TEXT NOT NULL, outcome TEXT,"
                " started_at INTEGER NOT NULL, ended_at INTEGER")
    if cost_cols:
        run_cols += (", model TEXT, tokens_in INTEGER, tokens_out INTEGER,"
                     " cost_usd REAL")
    con.execute(f"CREATE TABLE task_runs ({run_cols})")
    if verdicts:
        con.execute("CREATE TABLE task_verdicts (id INTEGER PRIMARY KEY"
                    " AUTOINCREMENT, task_id TEXT NOT NULL, target_task_id TEXT,"
                    " verdict TEXT NOT NULL, created_at INTEGER NOT NULL)")
    con.commit()
    con.close()
    return db


def _add_task(db: Path, tid: str, title: str, status: str, created_at: int,
              wake_deadline: int | None = None) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO tasks (id, title, status, created_at, wake_deadline)"
            " VALUES (?,?,?,?,?)", (tid, title, status, created_at, wake_deadline))
    except sqlite3.OperationalError:
        con.execute("INSERT INTO tasks (id, title, status, created_at)"
                    " VALUES (?,?,?,?)", (tid, title, status, created_at))
    con.commit()
    con.close()


def _add_link(db: Path, parent: str, child: str) -> None:
    con = sqlite3.connect(db)
    con.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)",
                (parent, child))
    con.commit()
    con.close()


def _add_run(db: Path, tid: str, *, profile: str = "worker",
             outcome: str = "completed", started_at: int = NOW - 100,
             ended_at: int | None = NOW - 50, model: str | None = None,
             tokens_in: int | None = None, tokens_out: int | None = None,
             cost_usd: float | None = None) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome,"
            " started_at, ended_at, model, tokens_in, tokens_out, cost_usd)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, profile, "done", outcome, started_at, ended_at, model,
             tokens_in, tokens_out, cost_usd))
    except sqlite3.OperationalError:
        con.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome,"
            " started_at, ended_at) VALUES (?,?,?,?,?,?)",
            (tid, profile, "done", outcome, started_at, ended_at))
    con.commit()
    con.close()


def _mk_heartbeat_db(home: Path, rows: list[tuple]) -> Path:
    """Engine-health board carrying ONLY cron_heartbeats (transition state)."""
    d = home / "kanban" / "boards" / "engine-health"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS cron_heartbeats ("
                "component TEXT PRIMARY KEY, last_beat INTEGER NOT NULL,"
                " next_beat_due INTEGER NOT NULL, status TEXT, detail TEXT)")
    con.executemany(
        "INSERT INTO cron_heartbeats (component, last_beat, next_beat_due,"
        " status, detail) VALUES (?,?,?,?,NULL) ON CONFLICT(component) DO UPDATE"
        " SET last_beat=excluded.last_beat, next_beat_due=excluded.next_beat_due,"
        " status=excluded.status", rows)
    con.commit()
    con.close()
    return db


def _iso(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _mk_jobs_json(home: Path, jobs: list[dict]) -> None:
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(
        json.dumps({"jobs": jobs, "updated_at": _iso(0)}), encoding="utf-8")


def _mk_backup(home: Path, age_seconds: int = 60) -> Path:
    d = home / "hermes-runtime" / "backups" / "kanban" / "alpha"
    d.mkdir(parents=True)
    f = d / "kanban.1.db"
    f.write_bytes(b"backup-bytes")
    ts = time.time() - age_seconds
    os.utime(f, (ts, ts))
    return f


def _mk_manifest(root: Path, board: str, tid: str, *, payload: bytes = b"evidence",
                 break_hash: bool = False, no_tarball: bool = False) -> Path:
    d = root / board / tid
    d.mkdir(parents=True)
    man: dict = {"version": 1, "task_id": tid, "board": board,
                 "archived_at": NOW, "file_count": 0, "files": [], "tarball": None}
    if not no_tarball:
        src = d / "payload.txt"
        src.write_bytes(payload)
        tar_path = d / "workspace.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(src, arcname="payload.txt")
        src.unlink()
        sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
        man["file_count"] = 1
        man["tarball"] = {"name": "workspace.tar.gz",
                          "size": tar_path.stat().st_size,
                          "sha256": ("0" * 64) if break_hash else sha}
    (d / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return d / "manifest.json"


@pytest.fixture()
def healthy_home(tmp_path, monkeypatch):
    """A fully green engine under tmp_path; engine_health module absent."""
    monkeypatch.delenv("HERMES_MISSION_ARTIFACTS_ROOT", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # Force the lazy-import fallback deterministically regardless of whether
    # the sibling agent has landed hermes_cli.engine_health yet.
    monkeypatch.setitem(sys.modules, "hermes_cli.engine_health", None)

    home = tmp_path / "hermes-home"
    _mk_heartbeat_db(home, [
        ("dispatcher-tick", NOW - 60, NOW + 600, "ok"),
        ("reconciler", NOW - 120, NOW + 900, "ok"),
    ])
    _mk_jobs_json(home, [
        {"id": "digest", "enabled": True, "next_run_at": _iso(3600),
         "last_status": "ok"},
        {"id": "retired", "enabled": False, "next_run_at": _iso(-864000),
         "last_status": "ok"},
    ])
    _mk_backup(home, age_seconds=60)
    db = _mk_board(home, "alpha")
    _add_task(db, "t_parent", "Build feature", "done", NOW - 10 * HOUR)
    _add_task(db, "t_fresh", "Implement follow-up", "todo", NOW - 1 * HOUR)
    _add_link(db, "t_parent", "t_fresh")
    _add_task(db, "t_block", "Waiting on human", "blocked", NOW - 1 * HOUR)
    _add_run(db, "t_parent", profile="seniorimplementationengineer",
             model="fable", tokens_in=1000, tokens_out=500, cost_usd=0.50)
    return home


# ── the tests ────────────────────────────────────────────────────────────────

class TestHealthyPane:
    def test_all_green_exit_zero(self, healthy_home, capsys):
        rc = mission_health.main(["--home", str(healthy_home)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "OVERALL: OK" in out
        for name in mission_health.SECTION_ORDER:
            assert name in out
        assert "FAIL" not in out
        assert len(out.strip().splitlines()) < 60

    def test_green_sections_are_one_line_each(self, healthy_home):
        data = mission_health.gather(healthy_home)
        assert data["ok"] is True
        pane = mission_health.render(data)
        # header + 7 section lines + overall footer == 9 (silence is signal)
        assert len(pane.splitlines()) == 9


class TestRenderingRule:
    def test_overdue_beat_fails_despite_ok_claim(self, healthy_home):
        _mk_heartbeat_db(healthy_home, [
            ("dispatcher-tick", NOW - 8 * 86400, NOW - 8 * 86400 + 300, "ok"),
        ])
        data = mission_health.gather(healthy_home)
        sec = data["sections"]["liveness"]
        assert sec["status"] == "FAIL"
        assert any("dispatcher-tick" in d and "overdue" in d for d in sec["detail"])
        assert data["ok"] is False

    def test_rule_applied_over_engine_health_module_output(
            self, healthy_home, monkeypatch):
        """Even when engine_health exists, its status claims are never trusted."""
        mod = types.ModuleType("hermes_cli.engine_health")
        mod.read_liveness = lambda home=None: [
            {"component": "reconciler", "last_beat": NOW - 7200,
             "next_beat_due": NOW - 3600, "status": "ok"},
            {"component": "sync-hermes-runtime", "last_beat": NOW - 60,
             "next_beat_due": NOW + 600, "status": "ok"},
        ]
        mod.read_cron_zombies = lambda home=None: []
        monkeypatch.setitem(sys.modules, "hermes_cli.engine_health", mod)
        sec = mission_health.gather(healthy_home)["sections"]["liveness"]
        assert sec["status"] == "FAIL"
        assert "engine_health" in sec["summary"]  # delegated source
        assert any("reconciler" in d for d in sec["detail"])
        assert not any("sync-hermes-runtime" in d for d in sec["detail"])

    def test_zombie_cron_jobs_fail_liveness(self, healthy_home):
        _mk_jobs_json(healthy_home, [
            {"id": "watchdog", "enabled": True, "next_run_at": _iso(-8 * 86400),
             "last_status": "ok"},  # the classic zombie-with-last:ok
            {"id": "digest", "enabled": True, "next_run_at": _iso(3600),
             "last_status": "ok"},
        ])
        sec = mission_health.gather(healthy_home)["sections"]["liveness"]
        assert sec["status"] == "FAIL"
        assert any("watchdog" in d for d in sec["detail"])


class TestBoardStalls:
    def test_deadzone_todo_detected(self, healthy_home):
        db = healthy_home / "kanban" / "boards" / "alpha" / "kanban.db"
        _add_task(db, "t_dead", "Ship report", "todo", NOW - 5 * HOUR)
        _add_link(db, "t_parent", "t_dead")           # parent done -> dead zone
        _add_task(db, "t_gated", "Later step", "todo", NOW - 5 * HOUR)
        _add_task(db, "t_open", "Open parent", "todo", NOW - 30 * HOUR)
        _add_link(db, "t_open", "t_gated")            # parent NOT done -> gated
        data = mission_health.gather(healthy_home)
        sec = data["sections"]["stalls"]
        assert sec["status"] == "FAIL"
        joined = "\n".join(sec["detail"])
        assert "t_dead" in joined
        assert "t_gated" not in joined
        # t_open is itself a parentless old todo == dead zone (vacuous truth)
        assert "t_open" in joined

    def test_blocked_over_12h_detected(self, healthy_home):
        db = healthy_home / "kanban" / "boards" / "alpha" / "kanban.db"
        _add_task(db, "t_oldblock", "Needs credentials", "blocked",
                  NOW - 13 * HOUR)
        sec = mission_health.gather(healthy_home)["sections"]["stalls"]
        assert sec["status"] == "FAIL"
        assert any("t_oldblock" in d and "blocked>12h" in d for d in sec["detail"])

    def test_wake_deadline_overdue_detected(self, healthy_home):
        db = healthy_home / "kanban" / "boards" / "alpha" / "kanban.db"
        _add_task(db, "t_awol", "Review gate", "in_progress", NOW - 1 * HOUR,
                  wake_deadline=NOW - 600)
        sec = mission_health.gather(healthy_home)["sections"]["stalls"]
        assert sec["status"] == "FAIL"
        assert any("t_awol" in d and "wake-overdue" in d for d in sec["detail"])

    def test_missing_wake_column_tolerated(self, healthy_home):
        _mk_board(healthy_home, "legacy", wake_col=False)
        data = mission_health.gather(healthy_home)
        assert data["sections"]["stalls"]["status"] == "OK"

    def test_corrupt_board_isolated_to_failure_line(self, healthy_home):
        broken = healthy_home / "kanban" / "boards" / "broken"
        broken.mkdir(parents=True)
        (broken / "kanban.db").write_bytes(b"this is not sqlite at all........")
        data = mission_health.gather(healthy_home)
        pane = mission_health.render(data)
        assert "broken" in pane           # surfaced, not swallowed
        assert "OVERALL: FAIL" in pane    # and it fails the pane
        # every other section still rendered
        for name in mission_health.SECTION_ORDER:
            assert name in pane


class TestBackups:
    def test_stale_backup_fails_and_exits_one(self, healthy_home, capsys):
        f = (healthy_home / "hermes-runtime" / "backups" / "kanban" / "alpha"
             / "kanban.1.db")
        ts = time.time() - 7 * HOUR
        os.utime(f, (ts, ts))
        rc = mission_health.main(["--home", str(healthy_home)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "FAIL backups" in out.replace("  ", " ")

    def test_missing_backup_dir_fails(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "hermes_cli.engine_health", None)
        monkeypatch.delenv("HERMES_MISSION_ARTIFACTS_ROOT", raising=False)
        sec = mission_health.gather(tmp_path / "empty-home")["sections"]["backups"]
        assert sec["status"] == "FAIL"


class TestEvidenceGauge:
    def test_broken_hash_drops_survival_below_100(self, healthy_home):
        root = healthy_home / "hermes-runtime" / "mission-artifacts"
        _mk_manifest(root, "alpha", "t_good", payload=b"good evidence")
        _mk_manifest(root, "alpha", "t_bad", payload=b"tampered", break_hash=True)
        _mk_manifest(root, "alpha", "t_empty", no_tarball=True)  # 0-file archive
        sec = mission_health.gather(healthy_home)["sections"]["evidence"]
        assert sec["status"] == "FAIL"
        assert sec["percent"] == 67  # 2 of 3 survive
        assert any("t_bad" in d and "sha256" in d for d in sec["detail"])

    def test_missing_tarball_detected(self, healthy_home):
        root = healthy_home / "hermes-runtime" / "mission-artifacts"
        mpath = _mk_manifest(root, "alpha", "t_gone")
        (mpath.parent / "workspace.tar.gz").unlink()
        sec = mission_health.gather(healthy_home)["sections"]["evidence"]
        assert sec["status"] == "FAIL"
        assert any("t_gone" in d and "missing" in d for d in sec["detail"])

    def test_all_surviving_is_green(self, healthy_home):
        root = healthy_home / "hermes-runtime" / "mission-artifacts"
        _mk_manifest(root, "alpha", "t_ok1")
        _mk_manifest(root, "alpha", "t_ok2", payload=b"other bytes")
        sec = mission_health.gather(healthy_home)["sections"]["evidence"]
        assert sec["status"] == "OK"
        assert "100%" in sec["summary"]


class TestCostAndCoverage:
    def test_sums_and_coverage(self, healthy_home):
        db = healthy_home / "kanban" / "boards" / "alpha" / "kanban.db"
        _add_run(db, "t_parent", model="sentra", tokens_in=2000,
                 tokens_out=1000, cost_usd=0.25)
        _add_run(db, "t_parent", model=None)  # run without model telemetry
        sec = mission_health.gather(healthy_home)["sections"]["cost"]
        assert sec["status"] == "OK"
        assert sec["tokens_in"] == 3000
        assert sec["tokens_out"] == 1500
        assert sec["cost_usd"] == pytest.approx(0.75)
        assert sec["coverage_pct"] == 67  # 2 of 3 runs carry a model

    def test_boards_without_cost_columns_tolerated(self, healthy_home):
        legacy = _mk_board(healthy_home, "legacy", cost_cols=False)
        _add_run(legacy, "t_x", profile="worker")
        data = mission_health.gather(healthy_home)
        sec = data["sections"]["cost"]
        assert sec["status"] == "OK"
        assert "without cost columns" in sec["summary"]
        assert data["ok"] is True  # tolerance, not failure


class TestOverhead:
    def test_alarm_above_15_percent(self, healthy_home):
        db = _mk_board(healthy_home, "fable-board")
        for i in range(7):
            _add_task(db, f"t_w{i}", f"Implement widget {i}", "todo", NOW - 60)
        _add_task(db, "t_g1", "Gate: verify handoff", "todo", NOW - 60)
        _add_task(db, "t_g2", "Cure routing loop", "todo", NOW - 60)
        _add_task(db, "t_g3", "Hygiene sweep", "todo", NOW - 60)
        _add_task(db, "t_arch", "Re-gate everything", "archived", NOW - 60)
        sec = mission_health.gather(healthy_home)["sections"]["overhead"]
        assert sec["status"] == "FAIL"
        assert any("fable-board" in d and "30%" in d for d in sec["detail"])

    def test_under_threshold_is_green(self, healthy_home):
        sec = mission_health.gather(healthy_home)["sections"]["overhead"]
        assert sec["status"] == "OK"


class TestVerdictCoverage:
    def test_completions_without_verdicts_fail(self, healthy_home):
        db = _mk_board(healthy_home, "reviewed", verdicts=True)
        _add_run(db, "t_r1", profile="qaverificationwarden")
        sec = mission_health.gather(healthy_home)["sections"]["verdicts"]
        assert sec["status"] == "FAIL"
        assert "0 verdicts / 1" in sec["summary"]

    def test_verdicts_recorded_is_green(self, healthy_home):
        db = _mk_board(healthy_home, "reviewed", verdicts=True)
        _add_run(db, "t_r1", profile="qaverificationwarden")
        con = sqlite3.connect(db)
        con.execute("INSERT INTO task_verdicts (task_id, target_task_id,"
                    " verdict, created_at) VALUES ('t_r1','t_r1','APPROVE',?)",
                    (NOW - 30,))
        con.commit()
        con.close()
        sec = mission_health.gather(healthy_home)["sections"]["verdicts"]
        assert sec["status"] == "OK"
        assert "1 verdicts / 1" in sec["summary"]

    def test_missing_table_everywhere_tolerated(self, healthy_home):
        sec = mission_health.gather(healthy_home)["sections"]["verdicts"]
        assert sec["status"] == "OK"
        assert "not deployed" in sec["summary"]


class TestPaneNeverCrashes:
    def test_totally_empty_home_still_renders(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "hermes_cli.engine_health", None)
        monkeypatch.delenv("HERMES_MISSION_ARTIFACTS_ROOT", raising=False)
        data = mission_health.gather(tmp_path / "nothing-here")
        pane = mission_health.render(data)
        assert "OVERALL: FAIL" in pane
        for name in mission_health.SECTION_ORDER:
            assert name in pane

    def test_render_survives_missing_section(self, healthy_home):
        data = mission_health.gather(healthy_home)
        del data["sections"]["evidence"]
        pane = mission_health.render(data)
        assert "section missing" in pane
        assert "OVERALL: FAIL" in pane
