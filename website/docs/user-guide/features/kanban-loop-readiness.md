---
sidebar_position: 14
title: "Kanban loop-readiness rubric"
description: "Audit checklist for deciding whether a Hermes Kanban workflow is safe to run repeatedly or unattended"
---

# Kanban loop-readiness rubric

Use this rubric before turning a one-off Kanban workflow into a recurring loop,
autonomous lane, or unattended automation. It is intentionally lightweight: read
one task tree with `hermes kanban show`, the dashboard, or the `kanban_*` tool
handoffs, then score the evidence the workflow already leaves behind.

The inspiration is `loop-audit` from
[cobusgreyling/loop-engineering](https://github.com/cobusgreyling/loop-engineering):
it scores projects on durable state, maker/checker split, safety gates, worktree
evidence, budget/run logs, and proof of recent loop activity. Hermes already has
a durable substrate in Kanban rows, comments, runs, events, workspaces, and
profile lanes, so this rubric maps those loop-readiness signals onto Hermes
surfaces instead of introducing a separate `STATE.md` / `LOOP.md` contract.

## Recommendation from the spike

Start as a docs-first rubric, not as a broad new production feature.

Hermes already has `hermes kanban diagnostics` for active distress signals such
as stranded ready tasks, protocol violations, and crash/retry evidence. Those
signals answer "what is broken right now?" Loop-readiness is a different,
slower question: "does this workflow leave enough evidence and gates to be safe
as a repeated loop?" Keep it as a documented operating checklist until multiple
real boards expose rules that are both stable and machine-checkable. If that
happens, add a small `hermes kanban diagnostics --readiness` extension that emits
this rubric as JSON/Markdown without changing task lifecycle semantics.

## Readiness levels

| Level | Meaning | Minimum bar |
|---|---|---|
| L0 | Draft | Task intent exists, but handoffs are mostly prose and retries require human archaeology. |
| L1 | Report-only loop | Tasks produce useful summaries, source/evidence links, and no external writes happen without a human. |
| L2 | Assisted execution | Workers can change files or create child tasks, but review-required gates, durable workspaces, and retry evidence are present. |
| L3 | Unattended-capable | The loop has clear human gates, budget/run-log controls, bounded retries, and recent successful activity evidence. |

Do not jump directly to L3 for production work. Run at L1 first, then L2 with a
reviewer, then consider L3 only after the workflow has boring run history.

## Audit checks

Score each check as `0` (missing), `1` (partial), or `2` (clear evidence). A
workflow is a good L2 candidate around 10/14. Treat L3 as blocked unless all
"required for L3" checks are present.

| Check | Evidence to look for | Required for L3 |
|---|---|---|
| `kanban_show` handoff quality | Task body, parent handoffs, comments, and prior attempts are sufficient for a fresh worker to continue without external context. | Yes |
| Structured closeout metadata | `kanban_complete` metadata or review-required comments include changed files, verification, decisions, residual risk, and source/evidence pointers. | Yes |
| Review-required convention | Code/docs/config-changing workers comment structured handoff metadata, then block with `review-required: ...` instead of marking unreviewed work done. | Yes for write loops |
| Durable workspace choice | Artifacts that must survive completion use `dir:<path>` or `worktree:<path>`; scratch workspaces are used only for disposable work. | Yes for artifact loops |
| Parent/child dependency shape | Orchestrator tasks link children with `parents=[...]` or `kanban_link`; fan-in tasks do not start until prerequisites are done. | Yes for multi-agent loops |
| Human gates and denylist | The task body or lane contract names operations that require a human: secrets, auth, payments, production infra, dependencies, PII, large file counts, or repeated failures. | Yes |
| Budget and run-log controls | Recurring loops have cadence, max runtime, max retries/spawns, token/cost estimate, and a place to inspect run history (`task_runs`, dashboard, or an external log). | Yes |
| Crash/retry evidence | Runs/events show crashes, protocol violations, reclaims, retries, or blocks in a way the next worker can diagnose; retry notes explain what changed. | Yes |

## Example review prompt

When reviewing a proposed loop, ask for a short readiness note in the task body
or as a comment:

```markdown
Loop readiness:
- Level target: L2 assisted execution
- Handoff evidence: changed_files + verification + residual_risk in metadata
- Workspace: worktree:/home/red/.hermes/hermes-agent/.worktrees/<task>
- Human gates: review-required for every code/docs change; no production pushes
- Budget/run log: max_runtime_seconds=3600, dashboard task_runs inspected weekly
- Retry rule: block after second failure with comments summarizing attempts
```

A worker can then use that note as a local operating contract, and a reviewer
can decide whether the workflow is ready to move from report-only to assisted
execution.

## Candidate CLI prototype (defer until rules stabilize)

If this becomes a CLI diagnostic, keep it read-only and additive:

```bash
hermes kanban diagnostics --readiness --task t_abcd --json
hermes kanban diagnostics --readiness --format md
```

Initial machine-checkable signals could include:

- recent task runs with summaries and metadata;
- `review-required:` blocked states or comments on changed-file handoffs;
- non-scratch workspace kind when required artifact paths are declared;
- parent links for synthesis/fan-in cards;
- crash/reclaim/protocol-violation events with subsequent comments;
- `max_runtime_seconds`, failure-limit, or retry notes for bounded loops.

Leave the subjective pieces — human gates, denylist quality, and whether the
handoff is understandable — in the docs checklist until there is enough real
board usage to encode them safely.

## Sources

- [`loop-audit` README](https://github.com/cobusgreyling/loop-engineering/blob/main/tools/loop-audit/README.md) — loop readiness score, levels, budget/run-log/activity signals.
- [`docs/safety.md`](https://github.com/cobusgreyling/loop-engineering/blob/main/docs/safety.md) — denylist, no-auto-merge default, least-privilege connectors, and human gates.
- [`docs/operating-loops.md`](https://github.com/cobusgreyling/loop-engineering/blob/main/docs/operating-loops.md) — cost budgeting, run logs, early exits, pause/kill criteria.
- [Kanban overview](./kanban) and [Kanban worker lanes](./kanban-worker-lanes) — Hermes task lifecycle, tool handoffs, workspaces, review-required convention, runs/events, and diagnostics.
