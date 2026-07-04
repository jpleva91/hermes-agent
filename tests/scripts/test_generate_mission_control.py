import json
import subprocess

from scripts import generate_mission_control as mission_control


VALID_GOVERNANCE = """
version: 1
spec_governed: true
spec_reference_required: true
spec_mode: full_spec_kit
canonical_spec: specs/001-clawta-hermes-agent-workflow/spec.md
allowed_reference_forms:
  - full_spec_kit_artifact_path
  - lite_spec_kit_mission_contract_path
  - legacy_migration_exception
override_authority: Jared
high_risk_requires_verifier: true
extensions:
  board_slug: clawta-hermes-agent-workflow
""".lstrip()


def test_run_command_records_safe_json_metadata_without_raw_previews(monkeypatch):
    raw_payload = {
        "task": {"id": "t_secret", "body": "raw task body must not leak"},
        "comments": [{"body": "raw comment must not leak"}],
        "runs": [],
    }

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(raw_payload),
            stderr=json.dumps({"body": "stderr body must not leak", "comments": []}),
        )

    monkeypatch.setattr(mission_control.subprocess, "run", fake_run)

    parsed, record = mission_control.run_command(["hermes", "kanban", "show", "--json", "t_secret"], json_output=True)

    assert parsed == raw_payload
    assert "stdout_preview" not in record
    assert "stderr_preview" not in record
    assert "stdout" not in record
    assert "stderr" not in record
    serialized = json.dumps(record, sort_keys=True)
    assert "raw task body must not leak" not in serialized
    assert "raw comment must not leak" not in serialized
    assert "stderr body must not leak" not in serialized
    assert record["stdout_summary"] == {
        "format": "json",
        "top_level_type": "object",
        "top_level_keys": ["comments", "runs", "task"],
        "top_level_count": 3,
        "status": None,
        "counts": {"comments": 1, "runs": 0},
    }
    assert record["stderr_summary"] == {
        "format": "json",
        "top_level_type": "object",
        "top_level_keys": ["body", "comments"],
        "top_level_count": 2,
        "status": None,
        "counts": {"comments": 0},
    }


def test_run_command_records_text_metadata_without_raw_text_previews(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="NAME DISK COUNTS\nposcoding yes running=1\n",
            stderr="body: should not persist raw stderr\ncomments: should not persist\n",
        )

    monkeypatch.setattr(mission_control.subprocess, "run", fake_run)

    output, record = mission_control.run_command(["hermes", "kanban", "assignees"])

    assert output == "NAME DISK COUNTS\nposcoding yes running=1\n"
    assert "stdout_preview" not in record
    assert "stderr_preview" not in record
    assert "body: should not persist raw stderr" not in json.dumps(record)
    assert "comments: should not persist" not in json.dumps(record)
    assert record["stdout_summary"] == {
        "format": "text",
        "line_count": 2,
        "char_count": len("NAME DISK COUNTS\nposcoding yes running=1\n"),
    }
    assert record["stderr_summary"] == {
        "format": "text",
        "line_count": 2,
        "char_count": len("body: should not persist raw stderr\ncomments: should not persist\n"),
    }


def test_snapshot_reports_spec_kit_layer_1_and_governance_status(tmp_path, monkeypatch):
    spec_dir = tmp_path / "specs" / "001-clawta-hermes-agent-workflow"
    spec_dir.mkdir(parents=True)
    (spec_dir / "governance.yaml").write_text(VALID_GOVERNANCE, encoding="utf-8")

    def fake_run_command(args, *, json_output=False):
        command = " ".join(args)
        record = {
            "command": command,
            "returncode": 0,
            "stdout_summary": {"format": "empty", "line_count": 0, "char_count": 0},
            "stderr_summary": {"format": "empty", "line_count": 0, "char_count": 0},
        }
        if " stats " in f" {command} ":
            return {"by_status": {"ready": 1}, "by_assignee": {"poscoding": 1}}, record
        if " list " in f" {command} ":
            return [], record
        if " assignees" in command:
            return "poscoding yes ready=1\n", record
        if " profile list" in command:
            return "poscoding\n", record
        return None, record

    monkeypatch.setattr(mission_control, "run_command", fake_run_command)

    snapshot = mission_control.build_snapshot(
        "clawta-hermes-agent-workflow",
        spec_dir,
        tmp_path / "mission-control.snapshot.json",
        tmp_path / "mission-control.html",
    )
    html = mission_control.render_dashboard(snapshot)

    assert snapshot["workflow"]["layers"][0]["name"] == "Spec Kit"
    assert snapshot["governance"]["ok"] is True
    assert snapshot["governance"]["errors"] == []
    assert "Spec Kit governance" in html
    assert "Layer 1" in html
    assert "governance.yaml" in html
