#!/usr/bin/env python3
"""Generate the local read-only Hermes Mission Control dashboard.

This collector is intentionally static/read-only: it shells out to existing
Hermes CLI read commands, writes a JSON snapshot, and renders standalone HTML.
It does not start services, mutate Kanban, create cron jobs, or publish files.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from plugins.kanban.governance import status_payload, validate_governance_path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC_DIR = REPO_ROOT / "specs" / "001-clawta-hermes-agent-workflow"
# The Kanban worker profile rewrites HOME for isolation, but T012 selected the
# host-level status-page directory explicitly. Keep these defaults pinned to the
# approved local artifact path rather than Path.home().
DEFAULT_STATUS_PUBLIC = Path("/home/red/.hermes/status-page/public")
DEFAULT_OUT_JSON = DEFAULT_STATUS_PUBLIC / "mission-control.snapshot.json"
DEFAULT_OUT_HTML = DEFAULT_STATUS_PUBLIC / "mission-control.html"
TAILNET_URL = "https://chimera-ant.tailf42b9f.ts.net:10001/mission-control.html"
SNAPSHOT_SCHEMA_VERSION = "mission-control.v1"
SELECTED_TASK_IDS = [
    "t_5dc820d7",
    "t_1f49e3ac",
    "t_acce2777",
    "t_394b7a46",
    "t_312cf127",
    "t_c8b3e920",
    "t_55f72500",
]
LANE_POLICIES = {
    "posresearch": "research lane; auto-spawn safe when evidence requirements are explicit",
    "possynthesis": "synthesis/planning lane; auto-spawn safe for local comments/handoffs",
    "poscoding": "implementation lane; local writes only, review-required before done",
    "posops": "ops verification lane; verify-only unless separately authorized",
    "posreview": "fresh-context review lane; review-only approval/blocker verdicts",
}
RISK_CATALOG = [
    {
        "risk": "Kanban CLI JSON schema drift",
        "mitigation": "Collector records command status and degrades missing fields to null/empty values.",
        "owner": "T013/T014",
    },
    {
        "risk": "Stale static snapshot",
        "mitigation": "Dashboard displays generated_at and the exact manual refresh command prominently.",
        "owner": "T013",
    },
    {
        "risk": "Scope creep into live dashboard/plugin/API/cron",
        "mitigation": "Generated artifact is static HTML+JSON only; future cron remains approval-gated.",
        "owner": "Jared",
    },
    {
        "risk": "Sensitive data leakage from board comments",
        "mitigation": "Snapshot stores handles and truncated summaries rather than raw long comment bodies.",
        "owner": "T013/T016",
    },
]


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def summarize_command_stream(text: str) -> dict[str, Any]:
    """Return safe stream metadata without persisting raw command output."""
    if not text:
        return {"format": "empty", "line_count": 0, "char_count": 0}

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {
            "format": "text",
            "line_count": len(text.splitlines()),
            "char_count": len(text),
        }

    if isinstance(payload, dict):
        status = payload.get("status")
        if status is None and isinstance(payload.get("task"), dict):
            status = payload["task"].get("status")
        counts = {
            key: len(value)
            for key, value in payload.items()
            if isinstance(value, (list, dict)) and key not in {"task"}
        }
        return {
            "format": "json",
            "top_level_type": "object",
            "top_level_keys": sorted(str(key) for key in payload.keys()),
            "top_level_count": len(payload),
            "status": status,
            "counts": counts,
        }

    if isinstance(payload, list):
        status_counts = Counter(
            item.get("status")
            for item in payload
            if isinstance(item, dict) and item.get("status")
        )
        return {
            "format": "json",
            "top_level_type": "array",
            "top_level_count": len(payload),
            "status_counts": dict(sorted(status_counts.items())),
        }

    return {
        "format": "json",
        "top_level_type": type(payload).__name__,
        "top_level_count": 1,
    }


def run_command(args: list[str], *, json_output: bool = False) -> tuple[Any, dict[str, Any]]:
    proc = subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    record: dict[str, Any] = {
        "command": " ".join(args),
        "returncode": proc.returncode,
        "stdout_summary": summarize_command_stream(proc.stdout),
        "stderr_summary": summarize_command_stream(proc.stderr),
    }
    if json_output and proc.returncode == 0:
        try:
            return json.loads(proc.stdout), record
        except json.JSONDecodeError as exc:
            record["parse_error"] = str(exc)
    return None if json_output else proc.stdout, record


def kanban_cmd(board: str, *parts: str) -> list[str]:
    return [sys.executable, "-m", "hermes_cli.main", "kanban", "--board", board, *parts]


def parse_assignees(output: str) -> dict[str, dict[str, Any]]:
    lanes: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        m = re.match(r"^(?P<name>[A-Za-z0-9_-]+)\s+(?P<disk>yes|no)\s+(?P<counts>.*)$", line.strip())
        if not m or m.group("name") == "NAME":
            continue
        counts: dict[str, int] = {}
        raw_counts = m.group("counts").strip()
        if raw_counts != "(idle)":
            for item in raw_counts.split(","):
                key, _, value = item.strip().partition("=")
                if key and value.isdigit():
                    counts[key] = int(value)
        lanes[m.group("name")] = {"on_disk": m.group("disk") == "yes", "counts": counts}
    return lanes


def parse_profiles(output: str) -> set[str]:
    profiles: set[str] = set()
    for raw in output.splitlines():
        line = raw.strip().lstrip("◆").strip()
        if not line or line.startswith("Profile") or line.startswith("─"):
            continue
        profiles.add(line.split()[0])
    return profiles


def extract_side_effect_authority(body: str) -> str | None:
    for line in (body or "").splitlines():
        if "Side-effect authority:" in line:
            return line.split("Side-effect authority:", 1)[1].strip()
    return None


def compact_text(value: str | None, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def card_from_show(show: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    task = show.get("task") or fallback or {}
    workspace = task.get("workspace_path") or ""
    if task.get("workspace_kind"):
        workspace = f"{task.get('workspace_kind')}:{workspace}" if workspace else task.get("workspace_kind")
    comments = show.get("comments") or []
    runs = show.get("runs") or []
    evidence_handles: list[dict[str, str]] = []
    for idx, comment in enumerate(comments, start=1):
        evidence_handles.append(
            {
                "type": "comment",
                "handle": f"kanban:{task.get('id')} comment {idx}",
                "summary": compact_text(comment.get("body"), 260),
            }
        )
    for run in runs:
        if run.get("summary") or run.get("outcome") or run.get("status"):
            evidence_handles.append(
                {
                    "type": "run",
                    "handle": f"kanban:{task.get('id')} run {run.get('id')}",
                    "summary": compact_text(run.get("summary") or run.get("outcome") or run.get("status"), 260),
                }
            )
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "assignee": task.get("assignee"),
        "priority": task.get("priority"),
        "parents": show.get("parents") or [],
        "children": show.get("children") or [],
        "workspace": workspace,
        "side_effect_authority": extract_side_effect_authority(task.get("body") or ""),
        "evidence_handles": evidence_handles,
        "latest_summary": show.get("latest_summary"),
    }


def build_snapshot(board: str, spec_dir: Path, out_json: Path, out_html: Path) -> dict[str, Any]:
    command_records: list[dict[str, Any]] = []
    stats, rec = run_command(kanban_cmd(board, "stats", "--json"), json_output=True)
    command_records.append(rec)
    task_list, rec = run_command(kanban_cmd(board, "list", "--json", "--archived", "--sort", "created"), json_output=True)
    command_records.append(rec)
    assignees_text, rec = run_command(kanban_cmd(board, "assignees"))
    command_records.append(rec)
    profiles_text, rec = run_command([sys.executable, "-m", "hermes_cli.main", "profile", "list"])
    command_records.append(rec)

    list_by_id = {task.get("id"): task for task in task_list or []}
    cards: list[dict[str, Any]] = []
    missing_fields: list[str] = []
    for task_id in SELECTED_TASK_IDS:
        show, rec = run_command(kanban_cmd(board, "show", "--json", task_id), json_output=True)
        command_records.append(rec)
        if show:
            cards.append(card_from_show(show, list_by_id.get(task_id)))
        elif task_id in list_by_id:
            missing_fields.append(f"show_json_unavailable:{task_id}")
            cards.append(card_from_show({"task": list_by_id[task_id], "parents": [], "children": []}))
        else:
            missing_fields.append(f"task_missing:{task_id}")

    lanes_from_assignees = parse_assignees(assignees_text or "")
    profile_names = parse_profiles(profiles_text or "")
    lanes = []
    for profile in sorted(set(LANE_POLICIES) | set(lanes_from_assignees)):
        lane_state = lanes_from_assignees.get(profile, {})
        lanes.append(
            {
                "profile": profile,
                "on_disk": bool(lane_state.get("on_disk")) or profile in profile_names,
                "counts": lane_state.get("counts", {}),
                "policy": LANE_POLICIES.get(profile, "verified profile; no board-specific policy recorded"),
            }
        )

    status_counts = (stats or {}).get("by_status") or dict(Counter(t.get("status") for t in task_list or [] if t.get("status")))
    assignee_counts = (stats or {}).get("by_assignee") or {}
    wave_status = {card.get("id"): card.get("status") for card in cards}
    evidence = []
    for card in cards:
        for handle in card.get("evidence_handles", []):
            evidence.append({"task_id": card.get("id"), **handle})
    evidence.extend(
        [
            {
                "task_id": "t_312cf127",
                "type": "file",
                "handle": str(Path(__file__).resolve()),
                "summary": "Static dashboard generator script.",
            },
            {
                "task_id": "t_312cf127",
                "type": "file",
                "handle": str(out_json),
                "summary": "Generated Mission Control JSON snapshot.",
            },
            {
                "task_id": "t_312cf127",
                "type": "file",
                "handle": str(out_html),
                "summary": "Generated standalone Mission Control HTML dashboard.",
            },
        ]
    )

    failed_commands = [c for c in command_records if c.get("returncode") != 0 or c.get("parse_error")]
    governance = status_payload(validate_governance_path(spec_dir))
    risks = list(RISK_CATALOG)
    if not governance.get("ok"):
        risks.append(
            {
                "risk": "Spec Governance sidecar is missing or invalid",
                "mitigation": "; ".join(governance.get("errors") or ["inspect governance.yaml"]),
                "owner": "Jared/poscoding",
            }
        )
    if governance.get("warnings"):
        risks.append(
            {
                "risk": "Spec Governance sidecar has warnings",
                "mitigation": "; ".join(governance.get("warnings") or []),
                "owner": "Jared/poscoding",
            }
        )
    if failed_commands:
        risks.append(
            {
                "risk": "One or more source commands failed or produced invalid JSON",
                "mitigation": "Inspect snapshot source.commands; dashboard renders available partial data only.",
                "owner": "T013/T014",
            }
        )
    if missing_fields:
        risks.append(
            {
                "risk": "Some expected task details were missing from the CLI data contract",
                "mitigation": ", ".join(missing_fields),
                "owner": "T013/T014",
            }
        )

    regenerate_command = (
        "cd /home/red/.hermes/hermes-agent && "
        "python scripts/generate_mission_control.py --board clawta-hermes-agent-workflow "
        "--out-json /home/red/.hermes/status-page/public/mission-control.snapshot.json "
        "--out-html /home/red/.hermes/status-page/public/mission-control.html"
    )
    board_db = os.environ.get("HERMES_KANBAN_DB")
    if not board_db or board not in board_db:
        board_db = str(Path("/home/red/.hermes/kanban/boards") / board / "kanban.db")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "source": {
            "board": board,
            "board_db": board_db,
            "spec_dir": str(spec_dir),
            "commands": command_records,
            "regenerate_command": regenerate_command,
        },
        "boards": {
            "status_counts": status_counts,
            "assignee_counts": assignee_counts,
            "oldest_ready_age_seconds": (stats or {}).get("oldest_ready_age_seconds"),
            "health": "partial" if failed_commands else "ok",
        },
        "workflow": {
            "name": "Clawta/Hermes Agent Workflow Mission Control",
            "root_task": "t_5dc820d7",
            "spec_path": str(spec_dir),
            "tailnet_url": TAILNET_URL,
            "layers": [
                {
                    "index": 1,
                    "name": "Spec Kit",
                    "path": str(spec_dir),
                    "governance_path": governance.get("path"),
                    "governance_ok": governance.get("ok"),
                },
                {"index": 2, "name": "Kanban board", "board": board},
                {"index": 3, "name": "Mission Control status artifact", "path": str(out_html)},
            ],
            "wave": ["t_1f49e3ac", "t_acce2777", "t_394b7a46", "t_312cf127"],
            "wave_status": wave_status,
        },
        "governance": governance,
        "cards": cards,
        "lanes": lanes,
        "evidence": evidence,
        "approval_queue": [
            {
                "decision": "Review generated local files before downstream synthesis/review treats implementation as accepted",
                "owner": "posreview/T016",
                "status": "required",
                "reason": "Code/local artifact change from T013 should receive fresh-context review.",
            },
            {
                "decision": "Merge, deploy, public publish, cron creation, account action, or external write",
                "owner": "Jared",
                "status": "required",
                "reason": "Explicitly outside the approved local-only static dashboard boundary.",
            },
        ],
        "risks": risks,
        "known_caveats": [
            "Dashboard is a static snapshot; rerun the manual command to refresh.",
            "Long Kanban comments are intentionally truncated to reduce sensitive-data exposure.",
            "No live Tailnet availability check is performed by this generator.",
        ],
        "side_effect_status": "local writes only: generator script plus selected JSON/HTML outputs; no Kanban mutation, cron, server, merge, deploy, or public publish",
    }


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def badge(value: Any) -> str:
    klass = re.sub(r"[^a-z0-9_-]", "-", str(value or "unknown").lower())
    return f"<span class='badge {esc(klass)}'>{esc(value or 'unknown')}</span>"


def render_dashboard(snapshot: dict[str, Any]) -> str:
    cards = snapshot.get("cards", [])
    lanes = snapshot.get("lanes", [])
    evidence = snapshot.get("evidence", [])
    approvals = snapshot.get("approval_queue", [])
    risks = snapshot.get("risks", [])
    governance = snapshot.get("governance", {})
    workflow_layers = snapshot.get("workflow", {}).get("layers") or []
    layer_items = "".join(
        f"<li><b>Layer {esc(layer.get('index'))}: {esc(layer.get('name'))}</b> "
        f"<code>{esc(layer.get('path') or layer.get('board'))}</code></li>"
        for layer in workflow_layers
    )
    governance_messages = (governance.get("errors") or []) + (governance.get("warnings") or [])
    governance_details = "".join(f"<li>{esc(item)}</li>" for item in governance_messages) or "<li>No errors or warnings.</li>"
    status_tiles = "".join(
        f"<div class='tile'><b>{esc(k)}</b><strong>{esc(v)}</strong></div>"
        for k, v in sorted((snapshot.get("boards", {}).get("status_counts") or {}).items())
    )
    card_rows = "".join(
        "<tr>"
        f"<td><code>{esc(c.get('id'))}</code></td>"
        f"<td>{esc(c.get('title'))}</td>"
        f"<td>{badge(c.get('status'))}</td>"
        f"<td>{esc(c.get('assignee'))}</td>"
        f"<td>{esc(', '.join(c.get('parents') or [])) or '—'}</td>"
        f"<td>{esc(', '.join(c.get('children') or [])) or '—'}</td>"
        f"<td>{esc(c.get('side_effect_authority') or 'not recorded')}</td>"
        "</tr>"
        for c in cards
    )
    lane_cards = "".join(
        f"<article><h3>{esc(l.get('profile'))}</h3><p>{'on disk' if l.get('on_disk') else 'not on disk'}</p>"
        f"<pre>{esc(json.dumps(l.get('counts') or {}, sort_keys=True))}</pre><p>{esc(l.get('policy'))}</p></article>"
        for l in lanes
    )
    evidence_items = "".join(
        f"<li><code>{esc(e.get('task_id'))}</code> <b>{esc(e.get('type'))}</b> "
        f"<span>{esc(e.get('handle'))}</span><p>{esc(e.get('summary'))}</p></li>"
        for e in evidence[:80]
    )
    approval_items = "".join(
        f"<li>{badge(a.get('status'))}<b>{esc(a.get('decision'))}</b><p>{esc(a.get('owner'))}: {esc(a.get('reason'))}</p></li>"
        for a in approvals
    )
    risk_items = "".join(
        f"<li><b>{esc(r.get('risk'))}</b><p>{esc(r.get('mitigation'))} <em>Owner: {esc(r.get('owner'))}</em></p></li>"
        for r in risks
    )
    snapshot_json = json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True)
    embedded = json.dumps(snapshot, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Mission Control</title>
<style>
:root {{ color-scheme: dark; --bg:#071018; --panel:#101a28; --panel2:#0b1420; --line:#27364d; --text:#edf5ff; --muted:#9fb0c6; --accent:#7dd3fc; --good:#86efac; --warn:#fde68a; --bad:#fca5a5; --violet:#c4b5fd; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:radial-gradient(circle at top left,#1d3154 0,#071018 36rem); color:var(--text); font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }}
header {{ padding:32px clamp(18px,4vw,60px); border-bottom:1px solid var(--line); background:rgba(7,16,24,.88); position:sticky; top:0; backdrop-filter:blur(14px); z-index:3; }}
h1 {{ margin:0; font-size:clamp(34px,6vw,72px); letter-spacing:-.06em; }}
.sub, p, li, td {{ color:var(--muted); }}
main {{ max-width:1500px; margin:0 auto; padding:26px clamp(18px,4vw,60px) 80px; }}
section {{ border:1px solid var(--line); background:linear-gradient(180deg,rgba(16,26,40,.96),rgba(11,20,32,.96)); border-radius:26px; padding:22px; margin:0 0 22px; box-shadow:0 18px 55px rgba(0,0,0,.28); }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; }}
.tile, article {{ border:1px solid var(--line); border-radius:18px; background:rgba(7,16,24,.52); padding:15px; }}
.tile b {{ display:block; color:var(--muted); text-transform:uppercase; letter-spacing:.1em; font-size:12px; }}
.tile strong {{ font-size:34px; }}
table {{ width:100%; border-collapse:collapse; }}
th,td {{ border-bottom:1px solid var(--line); padding:10px; text-align:left; vertical-align:top; }}
th {{ color:var(--accent); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }}
.table-wrap {{ overflow:auto; }}
.badge {{ display:inline-block; border:1px solid var(--line); border-radius:999px; padding:4px 9px; background:#0a1220; color:var(--accent); white-space:nowrap; }}
.badge.done, .badge.ok, .badge.not_required {{ color:var(--good); }} .badge.running, .badge.required {{ color:var(--warn); }} .badge.blocked, .badge.failed {{ color:var(--bad); }}
code, pre {{ font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
pre {{ white-space:pre-wrap; overflow:auto; max-height:460px; border:1px solid var(--line); border-radius:16px; padding:14px; background:#050b12; color:#d8e7ff; }}
a {{ color:var(--accent); }}
footer {{ color:var(--muted); padding-top:20px; }}
</style>
</head>
<body>
<header>
  <h1>Hermes Mission Control</h1>
  <div class="sub">Static, read-only snapshot for {esc(snapshot.get('workflow', {}).get('name'))}. Generated {esc(snapshot.get('generated_at'))}. Local/Tailnet only.</div>
</header>
<main>
  <section>
    <h2>Board status</h2>
    <div class="grid">{status_tiles}</div>
    <p>Board: <code>{esc(snapshot.get('source', {}).get('board'))}</code> · DB: <code>{esc(snapshot.get('source', {}).get('board_db'))}</code> · Health: {badge(snapshot.get('boards', {}).get('health'))}</p>
    <p>Manual refresh: <code>{esc(snapshot.get('source', {}).get('regenerate_command'))}</code></p>
    <p>Expected Tailnet/local path: <a href="{esc(snapshot.get('workflow', {}).get('tailnet_url'))}">{esc(snapshot.get('workflow', {}).get('tailnet_url'))}</a></p>
  </section>
  <section>
    <h2>Selected workflow graph</h2>
    <p>Spec: <code>{esc(snapshot.get('workflow', {}).get('spec_path'))}</code> · Root: <code>{esc(snapshot.get('workflow', {}).get('root_task'))}</code></p>
    <div class="table-wrap"><table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Lane</th><th>Parents</th><th>Children</th><th>Side-effect boundary</th></tr></thead><tbody>{card_rows}</tbody></table></div>
  </section>
  <section>
    <h2>Spec Kit governance</h2>
    <p>{badge('ok' if governance.get('ok') else 'failed')} Sidecar: <code>{esc(governance.get('path'))}</code></p>
    <ul>{layer_items}</ul>
    <p>Canonical spec: <code>{esc(governance.get('canonical_spec'))}</code> · Mode: <code>{esc(governance.get('spec_mode'))}</code> · Override authority: <code>{esc(governance.get('override_authority'))}</code></p>
    <ul>{governance_details}</ul>
  </section>
  <section>
    <h2>Worker lanes</h2>
    <div class="grid">{lane_cards}</div>
  </section>
  <section>
    <h2>Evidence / review handles</h2>
    <ul>{evidence_items}</ul>
  </section>
  <section>
    <h2>Approval queue</h2>
    <ul>{approval_items}</ul>
  </section>
  <section>
    <h2>Risks and caveats</h2>
    <ul>{risk_items}</ul>
    <h3>Raw snapshot</h3><pre id="snapshot">{esc(snapshot_json)}</pre>
  </section>
  <footer>No mutation controls are present. This file embeds its snapshot for standalone browser rendering and also writes mission-control.snapshot.json next to it.</footer>
</main>
<script type="application/json" id="mission-control-data">{embedded}</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate static Hermes Mission Control HTML + JSON snapshot.")
    parser.add_argument("--board", default="clawta-hermes-agent-workflow")
    parser.add_argument("--spec-dir", type=Path, default=DEFAULT_SPEC_DIR)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    parser.add_argument("--out-html", type=Path, default=DEFAULT_OUT_HTML)
    args = parser.parse_args(argv)

    snapshot = build_snapshot(args.board, args.spec_dir, args.out_json, args.out_html)
    atomic_write(args.out_json, json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    atomic_write(args.out_html, render_dashboard(snapshot))
    print(json.dumps({
        "ok": True,
        "schema_version": snapshot["schema_version"],
        "generated_at": snapshot["generated_at"],
        "out_json": str(args.out_json),
        "out_html": str(args.out_html),
        "tailnet_url": TAILNET_URL,
        "cards": len(snapshot.get("cards", [])),
        "lanes": len(snapshot.get("lanes", [])),
        "side_effect_status": snapshot["side_effect_status"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
