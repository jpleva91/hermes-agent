# Contract: Hermes Loop Pattern Registry

Status: design note only. Do not treat this as an implemented schema until an implementation card explicitly wires it into Hermes Kanban governance.

## Decision

Add a Hermes-native pattern registry contract that describes repeatable Kanban loops without turning the system into a static lane roster. The registry should be a machine-readable planning and audit artifact used by orchestrators, decomposers, dispatch preflights, docs, and optional CLI audits.

The registry complements, rather than replaces, existing Kanban truth:

- The board owns task lifecycle, dependency links, comments, runs, dispatcher events, and workspace paths.
- Profiles / external lane registrations own which assignees are currently spawnable.
- The registry owns reusable loop intent: when to run, what lane capabilities are eligible, what gates apply, what evidence is required, and when to no-op or stop.

## Source influences

- [loop-engineering `patterns/registry.yaml`](https://github.com/cobusgreyling/loop-engineering/blob/main/patterns/registry.yaml) defines a compact registry with `id`, `name`, `goal`, `cadence`, `risk`, `tools`, `skills`, `state`, `phases`, `human_gates`, `week_one_mode`, token-cost estimates, daily caps, and `early_exit_required`.
- [loop-engineering `docs/primitives.md`](https://github.com/cobusgreyling/loop-engineering/blob/main/docs/primitives.md) frames the minimum loop primitives as scheduling, worktree isolation, skills, connectors/MCP, maker/checker split, and durable memory/state.
- [loop-engineering `docs/operating-loops.md`](https://github.com/cobusgreyling/loop-engineering/blob/main/docs/operating-loops.md) adds production controls: token/cost budgeting, run logs, metrics, slow-down/pause/kill rules, and the L1 -> L2 -> L3 maturity path.
- [`website/docs/user-guide/features/kanban.md`](../../../website/docs/user-guide/features/kanban.md) defines the local substrate: board, task, parent/child `task_links`, comments, workspace kinds, dispatcher, tenant, worker tools, structured completion metadata, review-required blocking convention, dashboard, and auto/manual decomposition.
- [`website/docs/user-guide/features/kanban-worker-lanes.md`](../../../website/docs/user-guide/features/kanban-worker-lanes.md) defines lane identity as an assignee string resolved against Hermes profiles or explicit external lane registrations, with no arbitrary fallback.

## Contract shape

Store the registry as YAML or frontmatter-backed Markdown. A project may keep it under a Spec Kit contract path, a board governance sidecar, or an Obsidian operating note, but the `schema` and `patterns` list should stay stable.

```yaml
schema: hermes-loop-pattern-registry/v0.1
registry_id: personal-os-kanban-patterns
owner: Jared
scope:
  board: default
  tenant: null
  canonical_spec: specs/001-fable-emulation-workflow/spec.md
patterns:
  - id: daily-triage
    name: Daily Triage
    goal: Prioritized scan that creates or updates actionable Kanban cards.
    maturity: L1
    trigger:
      kind: schedule
      cadence: 1d
      event_source: cron
      no_op_check: "Exit if no new inbox/CI/calendar items since last run."
    risk: low
    eligible_lanes:
      capability_tags: [research, synthesis, ops]
      preferred_assignees: []
      excluded_assignees: []
      external_lane_kinds: []
    required_skills: [kanban-worker]
    state_artifact:
      kind: kanban_comment_or_markdown
      path: null
      required_fields: [last_seen_cursor, open_escalations, last_outcome]
    phases:
      - id: discover
        lane_capability: research
        evidence_required: [sources_or_queries]
      - id: triage
        lane_capability: synthesis
        evidence_required: [prioritized_findings]
      - id: route
        lane_capability: orchestrator
        evidence_required: [created_task_ids_or_noop_reason]
    human_gates:
      - id: external-write
        condition: side_effect_authority in [external-api, production]
        action: block_for_jared
      - id: review-required
        condition: code_or_config_changed
        action: block_with_review_required_prefix
    workspace:
      default_kind: scratch
      durable_required_when: [downstream_artifact_needed, code_change, state_file_update]
      isolation: board_pinned_workspace
    authority:
      side_effect_authority: docs/comments only
      approval_boundary: Jared approval required for merge/deploy/external production effects
    early_exit:
      required: true
      rule: "If discovery finds no actionable delta, append/run-log noop and do not spawn child cards."
    budget:
      token_cost_class: low
      tokens_noop: 5000
      tokens_report: 50000
      tokens_action: 200000
      suggested_daily_cap: 100000
      max_child_tasks_per_run: 3
    verification:
      maker_checker_required: false
      verification_actor: none
      evidence_expectations:
        - completion metadata includes findings/action/noop
        - comments cite source docs or commands
    failure_policy:
      max_attempts: 2
      fallback_policy: block
      pause_conditions: [false_positive_rate_high, cost_cap_near, repeated_escalation]
      kill_conditions: [cost_exceeds_value_two_weeks, team_mutes_notifications]
```

## Field mapping to Hermes concepts

| Registry field | Hermes source / consumer |
|---|---|
| `schema`, `registry_id`, `scope` | Spec Kit contract, board metadata, governance sidecar, or docs page. |
| `patterns[].id/name/goal` | Human-readable docs, orchestrator/decomposer prompt context, audit reports. |
| `trigger.kind/cadence/event_source` | Cron job, webhook subscription, manual `/kanban decompose`, dispatcher triage flow. |
| `risk`, `maturity` | Determines required review, budget strictness, and whether to start report-only. |
| `eligible_lanes.capability_tags` | Desired lane capabilities; orchestration resolves these against live profile descriptions and registered external lanes. |
| `eligible_lanes.preferred_assignees/excluded_assignees` | Optional hints only. They must be validated against `hermes profile list` or external lane registrations at runtime. |
| `required_skills` | Mapped to `kanban_create(..., skills=[...])` after checking the assignee has those skills installed. |
| `state_artifact` | Kanban comments, completion metadata, Markdown `STATE.md`, Obsidian note, or dedicated DB row. |
| `phases` | Candidate task graph nodes; dependencies become actual `task_links`, not prose-only ordering. |
| `human_gates` | `kanban_block(reason=...)`, review-required convention, dashboard status, or approval packet. |
| `workspace` | `workspace_kind`, `workspace_path`, per-board workspace root, worktree isolation. |
| `authority` | Task body side-effect authority and review/approval boundary. |
| `early_exit` | Worker prompt/audit requirement to no-op cheaply and avoid needless child tasks. |
| `budget` | Cron/orchestrator prompt limits, future cost audit, run-log summaries. |
| `verification` | Completion metadata requirements, maker/checker parent-child review cards, required evidence paths. |
| `failure_policy` | Max attempts, fallback to block/reassign, pause/kill notes for operators. |

## Non-static lane resolution

The registry must not become a hardcoded global roster like `researcher`, `writer`, `coder`, etc. It should describe capability requirements and optional preferences, then resolve at execution time:

1. Discover live Hermes profiles and descriptions.
2. Discover registered external lane kinds, if any.
3. Match `capability_tags`, required skills, workspace needs, and authority scope to eligible assignees.
4. If no safe match exists, route to the configured default assignee only for specification/triage, or block with a clear operator question.
5. Record the chosen lane in the task body/frontmatter and in completion metadata so audits can compare planned-vs-actual routing.

This keeps patterns portable across Jared's personal OS, repo-local boards, and future external workers. A pattern can say "needs synthesis + docs-only authority" without assuming the machine has a profile named `writer`.

## Dispatch and orchestration usage

Orchestrators/decomposers may use the registry as a decision aid:

- Select the smallest capable pattern for an incoming goal.
- Convert `phases` into Kanban cards only when a phase has real work.
- Attach required skills to child tasks.
- Choose workspace kind based on artifact durability and isolation requirements.
- Insert parent links only where a phase truly needs previous output.
- Enforce early-exit/no-op before spawning expensive children.
- Add review-required gates or Jared approval blocks when authority/risk requires it.

Dispatchers should not blindly execute registry rows. Registry metadata should feed preflight diagnostics and route selection; the board remains the state machine and the live profile/external-lane inventory remains the source of spawnability truth.

## Evidence and verification requirements

Every pattern should define enough evidence for downstream workers or Jared to verify the run without replaying all context:

- source citations, commands, changed paths, screenshots, or API handles as appropriate;
- task ids for child cards created by an orchestrator;
- no-op reason when the early-exit rule fires;
- residual risk and approval state;
- run-log fields for recurring loops: run id/time, pattern id, duration, items found, actions taken, escalations, estimated tokens/cost, outcome.

For code-changing or external-write loops, use maker/checker separation: implementation finishes with a review-required block, and a fresh reviewer or Jared gates completion.

## Migration path

1. Docs-only contract
   - Keep this document as the canonical v0.1 design.
   - Add example pattern entries for 1-2 real personal-OS loops.
   - Ask workers/orchestrators to cite the pattern id in task bodies and completion metadata.

2. Optional audit support
   - Add a `hermes kanban pattern audit <path>` or governance status command that validates required fields, unknown keys, stale assignee hints, missing required skills, absent early-exit rules, and budget gaps.
   - Audit should warn first. It should not block normal Kanban usage by default.

3. Governance/preflight integration
   - Allow a board governance sidecar to point to a canonical pattern registry.
   - For governed boards, preflight can require `pattern_id` and minimum evidence/authority fields on tasks.
   - Blocking mode should be opt-in per board, with warn/block/off modes like existing governance preflight.

4. Runtime/run-log integration
   - Record `pattern_id`, chosen lane, early-exit outcome, child task count, verification actor, and budget estimate in task events or completion metadata.
   - Expose these in dashboard diagnostics and weekly audit summaries.

5. CLI-assisted orchestration
   - Add a command that proposes a task graph from a pattern and live profile roster, but still requires human/orchestrator confirmation before creating cards.
   - Keep profile names dynamic; never bake a universal lane roster into the schema.

## Open questions for implementation cards

- Should canonical registries live in Spec Kit folders, board metadata, Obsidian, or all three with one declared source of truth?
- Should cost fields be estimates only, or should Hermes attach actual provider token usage when available?
- Should pattern-level `maturity` be manually set or inferred by audit from budget/run-log/review evidence?
- How should external lane plugins advertise capability tags and required auth without exposing secrets?
- What is the minimal dashboard surface: pattern id badges, audit warnings, or full pattern detail panels?
