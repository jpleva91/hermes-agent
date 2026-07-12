from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import stamp


def _home(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_verify_board_passes_when_completed_runs_are_stamped(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stamped", assignee="coder")
        assert kb.claim_task(conn, tid)
        assert kb.complete_task(
            conn,
            tid,
            metadata={
                "model": "gpt-5.5",
                "tokens_in": 100,
                "tokens_out": 20,
                "cached_tokens": 0,
                "cost_usd": 0.03,
                "billing_mode": "api",
                "wall_clock_seconds": 12.5,
            },
        )

    result = stamp.verify_board("default")

    assert result.ok is True
    assert result.checked_runs == 1
    assert result.stamped_runs == 1
    assert result.coverage == 1.0


def test_verify_board_fails_and_samples_legacy_rows(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="coder")
        now = 123
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'coder', 'done', 'completed', ?, ?)",
            (tid, now, now + 1),
        )

    result = stamp.verify_board("default")

    assert result.ok is False
    assert result.checked_runs == 1
    assert result.missing_runs == 1
    assert result.sample_missing[0]["task_id"] == tid
    assert "billing_mode" in result.sample_missing[0]["missing"]


def test_stamp_verify_cli_json_returns_nonzero_for_unstamped_rows(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="legacy", assignee="coder")
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'coder', 'done', 'completed', 1, 2)",
            (tid,),
        )

    code = stamp.main(["verify", "--board", "default", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload[0]["board"] == "default"
    assert payload[0]["missing_runs"] == 1


def test_verify_explicit_board_ignores_worker_db_pin(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as default_conn:
        default_tid = kb.create_task(default_conn, title="default", assignee="coder")
        assert kb.claim_task(default_conn, default_tid)
        assert kb.complete_task(
            default_conn,
            default_tid,
            metadata={
                "model": "gpt-5.5",
                "tokens_in": 10,
                "tokens_out": 5,
                "cached_tokens": 0,
                "cost_usd": 0.01,
                "billing_mode": "api",
                "wall_clock_seconds": 1.0,
            },
        )
    kb.create_board("other")
    other_db = kb.kanban_home() / "kanban" / "boards" / "other" / "kanban.db"
    with kb.connect(other_db) as other_conn:
        other_tid = kb.create_task(other_conn, title="other", assignee="coder")
        other_conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'coder', 'done', 'completed', 1, 2)",
            (other_tid,),
        )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(other_db))

    result = stamp.verify_board("default")

    assert result.board == "default"
    assert result.checked_runs == 1
    assert result.stamped_runs == 1
    assert result.missing_runs == 0


def test_verify_all_labels_match_queried_boards_under_worker_db_pin(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with kb.connect() as default_conn:
        default_tid = kb.create_task(default_conn, title="default", assignee="coder")
        assert kb.claim_task(default_conn, default_tid)
        assert kb.complete_task(
            default_conn,
            default_tid,
            metadata={
                "model": "gpt-5.5",
                "tokens_in": 10,
                "tokens_out": 5,
                "cached_tokens": 0,
                "cost_usd": 0.01,
                "billing_mode": "api",
                "wall_clock_seconds": 1.0,
            },
        )
    kb.create_board("other")
    other_db = kb.kanban_home() / "kanban" / "boards" / "other" / "kanban.db"
    with kb.connect(other_db) as other_conn:
        other_tid = kb.create_task(other_conn, title="other", assignee="coder")
        other_conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
            "VALUES (?, 'coder', 'done', 'completed', 1, 2)",
            (other_tid,),
        )
    monkeypatch.setenv("HERMES_KANBAN_DB", str(other_db))

    by_board = {row.board: row for row in stamp.verify_all()}

    assert by_board["default"].stamped_runs == 1
    assert by_board["default"].missing_runs == 0
    assert by_board["other"].stamped_runs == 0
    assert by_board["other"].missing_runs == 1


def test_verify_migrates_sparse_legacy_task_runs_table_without_crashing(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = home / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "task_id TEXT NOT NULL, status TEXT NOT NULL, started_at INTEGER NOT NULL)"
    )
    con.commit()
    con.close()

    result = stamp.verify_board("default")

    assert result.ok is True
    assert result.checked_runs == 0
    assert result.missing_columns == []
