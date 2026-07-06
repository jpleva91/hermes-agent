"""Tests for the P6 GOVERNANCE-AS-DATA modules (Mission Engine rework).

Covers :mod:`hermes_cli.autonomy_dial` (fail-closed loader, asymmetric dial
ops, down-trigger evaluation), :mod:`hermes_cli.constitutional_floor`
(sha256-pinned immutable floor, ``check`` API), and
:mod:`hermes_cli.rule_census` (census loader + coverage).

Hermetic by contract: everything runs against ``tmp_path`` + explicit paths;
the only exception is the census-artifact validation at the bottom, which is
skipped when the runtime spec tree is absent (CI).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest
import yaml

from hermes_cli import autonomy_dial as dial
from hermes_cli import constitutional_floor as floor
from hermes_cli import rule_census as census
from hermes_cli.autonomy_dial import MissionAutonomyDialError
from hermes_cli.constitutional_floor import MissionFloorError
from hermes_cli.rule_census import MissionRuleCensusError


@pytest.fixture(autouse=True)
def _reset_caches():
    dial.clear_cache()
    floor.clear_cache()
    yield
    dial.clear_cache()
    floor.clear_cache()


# ---------------------------------------------------------------------------
# Autonomy dial — loader fail-closed
# ---------------------------------------------------------------------------

def _dial_doc(level=1, clean=0, required=5, l3_locked=True):
    return {
        "status": "active",
        "level": level,
        "level_name": dial.LEVEL_NAMES[level],
        "ratchet": {
            "clean_missions_at_current_level": clean,
            "clean_missions_required_to_offer_up": required,
            "up_default": "NO",
            "down_triggers": list(dial.DEFAULT_DOWN_TRIGGERS),
        },
        "l3": {"locked": l3_locked},
    }


def _write_dial(path: Path, doc) -> Path:
    if isinstance(doc, str):
        path.write_text(doc, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def test_dial_fail_closed_on_corrupt_yaml(tmp_path, caplog):
    path = _write_dial(tmp_path / "autonomy.yaml", "level: [unclosed\n  {{{:::")
    with caplog.at_level(logging.ERROR, logger="hermes_cli.autonomy_dial"):
        state = dial.load_dial(path)
    assert state.fail_closed is True
    assert state.level == 0
    assert state.level_name == "OBSERVED"
    assert state.permissions == frozenset()
    assert state.down_triggers == dial.DEFAULT_DOWN_TRIGGERS
    assert any("AUTONOMY DIAL FAIL-CLOSED" in r.message for r in caplog.records)


def test_dial_fail_closed_on_missing_file(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger="hermes_cli.autonomy_dial"):
        state = dial.load_dial(tmp_path / "nope.yaml")
    assert state.fail_closed is True
    assert state.level == 0
    assert any("AUTONOMY DIAL FAIL-CLOSED" in r.message for r in caplog.records)


def test_dial_fail_closed_on_out_of_range_level(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", _dial_doc() | {"level": 9})
    state = dial.load_dial(path)
    assert state.fail_closed is True
    assert state.level == 0


def test_dial_fail_closed_on_non_mapping_doc(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", "- just\n- a\n- list\n")
    state = dial.load_dial(path)
    assert state.fail_closed is True


def test_dial_loads_valid_file(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", _dial_doc(level=2, clean=3))
    state = dial.load_dial(path)
    assert state.fail_closed is False
    assert state.level == 2
    assert state.level_name == "TRUSTED"
    assert state.allows("mission_closeout")
    assert not state.allows("below_floor_all")
    assert state.clean_missions == 3
    assert state.up_default == "NO"
    assert state.l3_locked is True


def test_dial_loads_real_shape_with_yaml11_no_boolean(tmp_path):
    # Real autonomy.yaml says `up_default: NO`, which YAML 1.1 parses as
    # boolean False — the loader must normalize it without warnings.
    path = tmp_path / "autonomy.yaml"
    path.write_text(
        "status: active\nlevel: 1\nlevel_name: SUPERVISED\n"
        "ratchet:\n  clean_missions_at_current_level: 0\n"
        "  clean_missions_required_to_offer_up: 5\n  next_offer_level: 2\n"
        "  up_default: NO\n  down_triggers:\n    - reversed_auto_decision\n"
        "    - watchdog_critical\nl3:\n  locked: true\n",
        encoding="utf-8",
    )
    state = dial.load_dial(path)
    assert state.fail_closed is False
    assert state.up_default == "NO"
    assert state.down_triggers == ("reversed_auto_decision", "watchdog_critical")


# ---------------------------------------------------------------------------
# Autonomy dial — decrement (auto-down)
# ---------------------------------------------------------------------------

def _dial_events(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT direction, from_level, to_level, reason, evidence_ref, authority, ts"
            " FROM dial_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def test_decrement_writes_event_and_lowers_level(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", _dial_doc(level=2, clean=4))
    db = tmp_path / "engine-health.db"
    state = dial.decrement(
        "watchdog critical: zombie crons", "t_watchdog01", path=path, db_path=db
    )
    assert state.level == 1
    assert state.level_name == "SUPERVISED"
    assert state.fail_closed is False

    rows = _dial_events(db)
    assert len(rows) == 1
    direction, from_level, to_level, reason, evidence_ref, authority, ts = rows[0]
    assert direction == "down"
    assert (from_level, to_level) == (2, 1)
    assert reason == "watchdog critical: zombie crons"
    assert evidence_ref == "t_watchdog01"
    assert authority == "engine:auto-down-trigger"
    assert isinstance(ts, int) and ts > 0

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["level"] == 1
    assert doc["level_name"] == "SUPERVISED"
    assert doc["ratchet"]["clean_missions_at_current_level"] == 0
    assert doc["last_down"]["reason"] == "watchdog critical: zombie crons"
    assert doc["last_down"]["evidence_ref"] == "t_watchdog01"


def test_decrement_at_floor_records_event_but_keeps_level(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", _dial_doc(level=0))
    before = path.read_bytes()
    db = tmp_path / "engine-health.db"
    state = dial.decrement("again", "t_evidence", path=path, db_path=db)
    assert state.level == 0
    rows = _dial_events(db)
    assert len(rows) == 1
    assert (rows[0][1], rows[0][2]) == (0, 0)
    assert path.read_bytes() == before  # no rewrite when level unchanged


def test_decrement_requires_reason_and_evidence(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", _dial_doc(level=1))
    with pytest.raises(MissionAutonomyDialError):
        dial.decrement("", "t_x", path=path, db_path=tmp_path / "db.sqlite")
    with pytest.raises(MissionAutonomyDialError):
        dial.decrement("reason", "  ", path=path, db_path=tmp_path / "db.sqlite")


# ---------------------------------------------------------------------------
# Autonomy dial — request_increment is human-only
# ---------------------------------------------------------------------------

def test_request_increment_never_changes_level(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", _dial_doc(level=1, clean=5))
    before = path.read_bytes()
    stub = dial.request_increment("5 clean missions", "digest-2026-07-05", path=path)
    assert path.read_bytes() == before  # no write, ever
    assert stub["level_changed"] is False
    assert stub["type"] == "jared_packet_stub"
    assert stub["default"] == "NO"
    assert stub["authority_required"] == "jared_packet"
    assert stub["from_level"] == 1 and stub["to_level"] == 2
    assert "silence = NO at next digest" in stub["question"]
    assert stub["eligible"] is True
    # And the file still loads at the original level.
    dial.clear_cache()
    assert dial.load_dial(path).level == 1


def test_request_increment_not_earned(tmp_path):
    path = _write_dial(tmp_path / "autonomy.yaml", _dial_doc(level=1, clean=2))
    stub = dial.request_increment(path=path)
    assert stub["eligible"] is False
    assert any(b.startswith("ratchet_not_earned:2/5") for b in stub["blocked_by"])


def test_request_increment_l3_overseer_block(tmp_path):
    path = _write_dial(
        tmp_path / "autonomy.yaml", _dial_doc(level=2, clean=5, l3_locked=True)
    )
    stub = dial.request_increment(path=path)
    assert stub["to_level"] == 3
    assert stub["eligible"] is False
    assert "l3_overseer_go_live_block" in stub["blocked_by"]


def test_request_increment_fail_closed_dial_is_not_eligible(tmp_path):
    stub = dial.request_increment(path=tmp_path / "missing.yaml")
    assert stub["eligible"] is False
    assert "dial_fail_closed" in stub["blocked_by"]
    assert stub["level_changed"] is False


# ---------------------------------------------------------------------------
# Autonomy dial — down-trigger evaluation
# ---------------------------------------------------------------------------

def test_evaluate_down_triggers_all_fire():
    triggered = dial.evaluate_down_triggers(
        {
            "circuit_breaker_fired": 1,
            "verdict_failclosed_hits": 3,
            "watchdog_criticals": 2,
            "cross_model_degradations": 1,
        }
    )
    assert sorted(triggered) == [
        "circuit_breaker_fired",
        "cross_model_degradation",
        "verdict_failclosed",
        "watchdog_critical",
    ]


def test_evaluate_down_triggers_zero_and_unknown_signals():
    assert dial.evaluate_down_triggers({}) == []
    assert dial.evaluate_down_triggers(
        {"circuit_breaker_fired": 0, "unknown_counter": 99}
    ) == []


def test_evaluate_down_triggers_malformed_counter_does_not_fire():
    assert dial.evaluate_down_triggers({"watchdog_criticals": "lots"}) == []
    assert dial.evaluate_down_triggers({"watchdog_criticals": None}) == []
    assert dial.evaluate_down_triggers("not-a-mapping") == []


def test_evaluate_down_triggers_partial():
    assert dial.evaluate_down_triggers({"watchdog_criticals": 1}) == ["watchdog_critical"]


# ---------------------------------------------------------------------------
# Constitutional floor — hash pin integrity
# ---------------------------------------------------------------------------

def _canonical_floor(tmp_path: Path) -> Path:
    path = tmp_path / "floor.yaml"
    path.write_text(floor.CANONICAL_FLOOR_TEXT, encoding="utf-8")
    return path


def test_embedded_canonical_text_matches_pin():
    digest = hashlib.sha256(floor.CANONICAL_FLOOR_TEXT.encode("utf-8")).hexdigest()
    assert digest == floor.FLOOR_SHA256


def test_floor_loads_canonical_copy(tmp_path):
    state = floor.load_floor(_canonical_floor(tmp_path))
    assert [item.id for item in state.items] == list(floor.FLOOR_IDS)
    assert len(state.items) == 8
    assert all(item.immutable for item in state.items)
    assert all(item.rule and item.enforcement_point for item in state.items)
    assert state.sha256 == floor.FLOOR_SHA256


def test_floor_tamper_raises(tmp_path):
    path = _canonical_floor(tmp_path)
    text = path.read_text(encoding="utf-8")
    # A one-character "amendment" — floor weakening attempt.
    path.write_text(text.replace("Never auto-approved", "Auto-approved"), encoding="utf-8")
    with pytest.raises(MissionFloorError, match="HASH MISMATCH"):
        floor.load_floor(path)


def test_floor_tamper_even_whitespace_raises(tmp_path):
    path = _canonical_floor(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(MissionFloorError, match="HASH MISMATCH"):
        floor.load_floor(path)


def test_floor_missing_fails_closed(tmp_path):
    with pytest.raises(MissionFloorError, match="fail closed"):
        floor.load_floor(tmp_path / "absent.yaml")


def test_floor_env_path_override_still_pinned(tmp_path):
    tampered = tmp_path / "floor.yaml"
    tampered.write_text("kind: mission_constitutional_floor\nitems: []\n", encoding="utf-8")
    env = {"HERMES_CONSTITUTIONAL_FLOOR_PATH": str(tampered)}
    with pytest.raises(MissionFloorError, match="HASH MISMATCH"):
        floor.load_floor(env=env)


def test_floor_has_no_kill_switch():
    # The floor is the floor: no env var may bypass the hash pin. Guard the
    # module against anyone "helpfully" adding one later.
    import inspect

    source = inspect.getsource(floor)
    for banned in ("DISABLE", "KILL", "SKIP_HASH", "FAILOPEN", "FAIL_OPEN"):
        assert banned not in source.upper().replace("KILL-SWITCH", ""), (
            f"constitutional_floor must not grow a {banned} escape hatch"
        )


# ---------------------------------------------------------------------------
# Constitutional floor — check() semantics
# ---------------------------------------------------------------------------

@pytest.fixture()
def floor_file(tmp_path):
    return _canonical_floor(tmp_path)


def _floor_id(excinfo):
    return excinfo.value.floor_id


def test_check_send_spend_require_jared_packet(floor_file):
    for cls in ("send", "spend"):
        with pytest.raises(MissionFloorError) as exc:
            floor.check("side_effect", {"side_effect_class": cls}, path=floor_file)
        assert _floor_id(exc) == "F001"
        assert "F001" in str(exc.value)
    assert (
        floor.check(
            "side_effect",
            {"side_effect_class": "send", "jared_packet_id": "pkt_123"},
            path=floor_file,
        )
        is None
    )


def test_check_deploy_merge_require_cross_model_and_packet(floor_file):
    for cls in ("deploy", "merge"):
        with pytest.raises(MissionFloorError) as exc:
            floor.check("complete_task", {"side_effect_class": cls}, path=floor_file)
        assert _floor_id(exc) == "F002"
        with pytest.raises(MissionFloorError) as exc:
            floor.check(
                "complete_task",
                {"side_effect_class": cls, "cross_model_approve": True},
                path=floor_file,
            )
        assert _floor_id(exc) == "F002"
        assert (
            floor.check(
                "complete_task",
                {
                    "side_effect_class": cls,
                    "cross_model_approve": True,
                    "jared_packet_id": "pkt_9",
                },
                path=floor_file,
            )
            is None
        )


def test_check_unknown_side_effect_class_fails_closed(floor_file):
    with pytest.raises(MissionFloorError) as exc:
        floor.check("side_effect", {"side_effect_class": "exfiltrate"}, path=floor_file)
    assert _floor_id(exc) == "F002"


def test_check_no_side_effect_class_passes(floor_file):
    assert floor.check("complete_task", {}, path=floor_file) is None


def test_check_waive_requires_jared_packet(floor_file):
    with pytest.raises(MissionFloorError) as exc:
        floor.check("waive", {}, path=floor_file)
    assert _floor_id(exc) == "F003"
    with pytest.raises(MissionFloorError) as exc:
        floor.check("verdict", {"verdict": "WAIVED"}, path=floor_file)
    assert _floor_id(exc) == "F003"
    assert floor.check("verdict", {"verdict": "APPROVE"}, path=floor_file) is None
    assert (
        floor.check("waive", {"waive_authority": "pkt_waive_1"}, path=floor_file) is None
    )


def test_check_git_push_blocks_nousresearch_origin(floor_file):
    with pytest.raises(MissionFloorError) as exc:
        floor.check(
            "git_push",
            {"remote_url": "git@github.com:NousResearch/hermes-agent.git"},
            path=floor_file,
        )
    assert _floor_id(exc) == "F004"
    assert (
        floor.check(
            "git_push",
            {"remote_url": "git@github.com:jpleva91/hermes-agent.git", "remote": "fork"},
            path=floor_file,
        )
        is None
    )


def test_check_dial_up_requires_jared_packet(floor_file):
    with pytest.raises(MissionFloorError) as exc:
        floor.check("dial_up", {}, path=floor_file)
    assert _floor_id(exc) == "F005"
    assert floor.check("dial_up", {"jared_packet_id": "pkt_up"}, path=floor_file) is None


def test_check_delete_requires_archive(floor_file):
    with pytest.raises(MissionFloorError) as exc:
        floor.check("delete_history", {"target": "board:geto"}, path=floor_file)
    assert _floor_id(exc) == "F006"
    assert (
        floor.check(
            "delete_history",
            {"target": "board:geto", "archive_ref": "mission-artifacts/geto/t_1"},
            path=floor_file,
        )
        is None
    )


def test_check_review_fallback_t2_same_model_silent_blocked(floor_file):
    with pytest.raises(MissionFloorError) as exc:
        floor.check("review_fallback", {"tier": "T2", "cross_model": False}, path=floor_file)
    assert _floor_id(exc) == "F007"
    # Declared fallback, cross-model, or T1 all pass.
    assert (
        floor.check(
            "review_fallback",
            {"tier": "T2", "cross_model": False, "fallback_declared": True},
            path=floor_file,
        )
        is None
    )
    assert (
        floor.check("review_fallback", {"tier": 2, "cross_model": True}, path=floor_file)
        is None
    )
    assert (
        floor.check("review_fallback", {"tier": "T1", "cross_model": False}, path=floor_file)
        is None
    )


def test_check_external_send_requires_receipt(floor_file):
    with pytest.raises(MissionFloorError) as exc:
        floor.check("external_send", {"channel": "discord"}, path=floor_file)
    assert _floor_id(exc) == "F008"
    assert (
        floor.check(
            "external_send",
            {"channel": "discord", "delivery_receipt": "msg_1522819933543731321"},
            path=floor_file,
        )
        is None
    )


def test_check_unknown_action_not_governed(floor_file):
    assert floor.check("make_coffee", {"strength": "espresso"}, path=floor_file) is None


def test_check_fails_closed_when_floor_tampered(tmp_path):
    path = _canonical_floor(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("F004", "F00X"), encoding="utf-8")
    with pytest.raises(MissionFloorError):
        floor.check("git_push", {"remote_url": "anything"}, path=path)


# ---------------------------------------------------------------------------
# Rule census — hermetic loader/coverage tests
# ---------------------------------------------------------------------------

def _census_doc(rules):
    return {"kind": "mission_rules_census", "version": 1, "rules": rules}


def _rule(rule_id="R001", source="spec.md:1", cls="advisory", point="—", clause="a MUST clause"):
    return {
        "rule_id": rule_id,
        "source": source,
        "clause": clause,
        "class": cls,
        "enforcement_point": point,
    }


def _write_census(tmp_path, doc):
    path = tmp_path / "rules-census.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def test_census_parses_and_coverage_computes(tmp_path):
    path = _write_census(
        tmp_path,
        _census_doc(
            [
                _rule("R001", "spec.md:23", "enforced", "kanban_db preflight"),
                _rule("R002", "spec.md:40", "measured", "lane registry"),
                _rule("R003", "spec.md:41"),
                _rule("R004", "spec.md:42"),
            ]
        ),
    )
    rules = census.load_census(path)
    assert [r.rule_id for r in rules] == ["R001", "R002", "R003", "R004"]
    assert rules[0].source_file == "spec.md" and rules[0].source_line == 23
    cov = census.coverage(rules)
    assert cov == {
        "total": 4,
        "enforced": 1,
        "pct_enforced": 25.0,
        "measured": 1,
        "pct_measured": 25.0,
        "advisory": 2,
        "pct_advisory": 50.0,
    }


def test_census_duplicate_rule_id_rejected(tmp_path):
    path = _write_census(
        tmp_path, _census_doc([_rule("R001"), _rule("R001", "spec.md:2")])
    )
    with pytest.raises(MissionRuleCensusError, match="duplicate rule_id"):
        census.load_census(path)


def test_census_invalid_class_rejected(tmp_path):
    path = _write_census(tmp_path, _census_doc([_rule(cls="aspirational")]))
    with pytest.raises(MissionRuleCensusError, match="class"):
        census.load_census(path)


def test_census_enforced_requires_enforcement_point(tmp_path):
    path = _write_census(tmp_path, _census_doc([_rule(cls="enforced", point="—")]))
    with pytest.raises(MissionRuleCensusError, match="enforcement_point"):
        census.load_census(path)


def test_census_bad_source_rejected(tmp_path):
    path = _write_census(tmp_path, _census_doc([_rule(source="spec.md")]))
    with pytest.raises(MissionRuleCensusError, match="file:line"):
        census.load_census(path)


def test_census_missing_file_rejected(tmp_path):
    with pytest.raises(MissionRuleCensusError, match="not readable"):
        census.load_census(tmp_path / "absent.yaml")


# ---------------------------------------------------------------------------
# Rule census — the real authored artifact (skipped where runtime tree absent)
# ---------------------------------------------------------------------------

_REAL_SPEC_DIR = Path("/home/red/.hermes/specs/002-mission-engine")
_REAL_CENSUS = _REAL_SPEC_DIR / "rules-census.yaml"


@pytest.mark.skipif(not _REAL_CENSUS.exists(), reason="runtime spec tree not present")
def test_real_census_parses_ids_unique_sources_exist():
    rules = census.load_census(_REAL_CENSUS)
    ids = [r.rule_id for r in rules]
    assert len(ids) == len(set(ids))  # loader enforces; belt and braces
    assert len(rules) >= 90
    missing = sorted(
        {r.source_file for r in rules if not (_REAL_SPEC_DIR / r.source_file).exists()}
    )
    assert missing == [], f"census cites nonexistent source files: {missing}"
    cov = census.coverage(rules)
    assert cov["enforced"] > 0
    assert cov["advisory"] > cov["enforced"]  # honesty check: most rules are words
    assert cov["total"] == len(rules)


@pytest.mark.skipif(
    not (_REAL_SPEC_DIR / "floor.yaml").exists(), reason="runtime spec tree not present"
)
def test_real_floor_file_matches_pin():
    state = floor.load_floor(_REAL_SPEC_DIR / "floor.yaml")
    assert [item.id for item in state.items] == list(floor.FLOOR_IDS)
