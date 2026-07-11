# Implementation Plan: Hermes-Native Discord-to-Spec-Kit Software Factory

**Branch**: `feature/discord-spec-factory-v1` | **Date**: 2026-07-10 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-discord-spec-factory/spec.md`

## Summary

Build a Hermes-native software factory where Discord is the intake and collaboration surface and repository-local Spec Kit artifacts are the canonical persisted state. This tranche is deliberately bounded: validate a fail-closed intake binding, build a source/context packet, load the repository-local Spec Kit workflow contract without executing an official engine, create/reuse one Discord workflow thread, and persist clarify/approval gate replies. Profile dispatch, Kanban mutation, and GitHub PR creation remain packet-only/future work.

## Technical Context

**Language/Version**: Python 3.11+ in the existing Hermes Agent repository

**Primary Dependencies**: Existing Hermes gateway/platform abstractions, Discord adapter, channel prompts/skill bindings, `create_handoff_thread`, Hermes profile metadata, packet builders for optional Kanban/GitHub projections, repository-local `.specify/workflows/speckit/workflow.yml`

**Storage**: Repository Spec Kit files under `specs/<feature>/`, plus future persisted workflow index/state in Hermes-managed storage scoped by `HERMES_HOME`; no live home writes in tests

**Testing**: pytest with isolated temp `HERMES_HOME`, synthetic Discord events, contract tests for packet/routing validation, no network credentials

**Target Platform**: Hermes Agent on Linux/macOS/Windows with Discord gateway enabled; local development in isolated worktree

**Project Type**: Hermes Agent core/gateway extension plus Spec Kit workflow pack

**Performance Goals**: Intake validation and source packet construction should complete synchronously in under 100ms for normal text/metadata-only synthetic events; long-running triage/execution is async and not on the gateway hot path

**Constraints**: Preserve prompt caching; fail closed on missing config/permissions/routes; do not add a broad core model tool; do not make Kanban canonical; avoid live config, live gateway restarts, remote git, or credentials in development/tests

**Scale/Scope**: V1 supports configured intake channels, one workflow thread per accepted request, required role route validation, optional Kanban/GitHub packet construction, and no live downstream dispatch/mutation.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The repository constitution template is unratified, so the operative gates are the repository `AGENTS.md` and Hermes contribution rubric:

- **Core narrow waist**: PASS — the design extends existing gateway/config/profile/spec surfaces and does not add a new always-present model tool.
- **Prompt caching**: PASS — channel prompts/skills are injected only through existing per-session/new-session paths; workflow state changes do not rewrite active conversation history.
- **Fail-closed permissions**: PASS — missing intake binding, missing route, unauthorized approval, and handoff credential absence block side effects.
- **Existing infrastructure first**: PASS — uses Discord channel prompts/skill bindings, `create_handoff_thread`, existing profile route validation, packet-only Kanban/GitHub contracts, and repository-local Spec Kit state.
- **E2E validation with temp homes**: PASS — quickstart and tasks require synthetic local tests before live gateway enablement.

## Project Structure

### Documentation (this feature)

```text
specs/001-discord-spec-factory/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── governance.yaml
├── contracts/
│   ├── intake-channel-config.yaml
│   ├── source-context-packet.schema.json
│   ├── role-routing-contract.yaml
│   ├── workflow-thread-contract.yaml
│   ├── pr-target-contract.yaml
│   └── kanban-projection-contract.yaml
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
hermes_cli/
├── discord_spec_factory.py          # steel-thread pure helpers: config validation, source packet, route checks
└── ...existing modules...

gateway/
├── platforms/base.py                # existing MessageEvent, channel prompt/skill helpers; no v1 primitive change required
├── run.py                           # future integration point for dispatching accepted factory intake
└── ...existing modules...

plugins/platforms/discord/adapter.py # existing Discord event/attachment/thread surfaces; future event hook point

tests/
└── hermes_cli/
    └── test_discord_spec_factory.py # local steel-thread tests
```

**Structure Decision**: Start with a pure `hermes_cli.discord_spec_factory` module because it can validate the contract and source packet locally without gateway side effects. Later tasks may integrate it into the Discord adapter/gateway once the contracts are green.

## Phase 0 Research Summary

See [research.md](./research.md). Key decisions: Spec Kit state is canonical, Discord threads are collaboration surfaces, Kanban is projection-only, role routing is explicit and fail-closed, attachment/voice handling references existing cached media/transcripts rather than inlining binaries.

## Phase 1 Design Summary

See [data-model.md](./data-model.md), [contracts/](./contracts/), and [quickstart.md](./quickstart.md). The MVP data path is:

1. Validate intake binding.
2. Capture source/context packet.
3. Derive workflow ID and thread title.
4. Load the repository-local Spec Kit workflow contract and initialize helper workflow state without official execution.
5. Post/use workflow thread.
6. Advance only through persisted gates.
7. Validate optional Kanban projection packets only; do not mutate Kanban from the gateway.
8. Validate GitHub handoff packets only; do not create branches/PRs from the gateway.

## Post-Design Constitution Check

- **Core narrow waist**: PASS — first steel thread is a pure helper + tests; no new tool schema.
- **No live side effects**: PASS — local quickstart uses temp files/synthetic events.
- **Kanban projection only**: PASS — contracts explicitly mark Kanban as derived and recoverable.
- **Permission gates**: PASS — governance and contracts require default deny for activation, approvals, routes, and PR handoff.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| None | N/A | N/A |
