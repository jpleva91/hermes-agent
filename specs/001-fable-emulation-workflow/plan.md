# Implementation Plan: Fable Emulation Workflow

**Branch**: `001-fable-emulation-workflow` | **Date**: 2026-06-13 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-fable-emulation-workflow/spec.md`

## Summary

Create a Hermes-native operating pattern that emulates the observable behavior Jared liked in banned/unavailable Fable 5: long-horizon planning, dynamic fan-out/fan-in, context isolation, verification, recovery, and procedural memory. The initial implementation is not a new plugin; it is a Spec Kit + Kanban + skills + Obsidian/Tailnet workflow that can later justify enforcement or convenience code.

## Technical Context

**Language/Version**: Python 3.11+ for Hermes internals and CLI scripts; Markdown for Spec Kit/Obsidian/skills; HTML for Tailnet reporting.

**Primary Dependencies**: Existing Hermes surfaces: Kanban plugin/CLI, gateway dispatcher, profiles, skills, cron, terminal/file tools, NotebookLM CLI integration, Obsidian vault, Tailnet status page.

**Storage**: Existing Hermes Kanban SQLite board(s), repository specs under `specs/`, Obsidian Markdown notes, Tailnet static HTML, Hermes skills under profile scope.

**Testing**: Spec validation checklist; CLI smoke checks; Kanban show/list/dispatch verification; targeted pytest only if code changes become necessary.

**Target Platform**: Linux host running Hermes gateway/Discord and local filesystem; Tailnet web status page.

**Project Type**: Agent workflow / CLI-operational architecture inside Hermes Agent repository.

**Performance Goals**: Initial grooming should keep the first board graph reviewable (roughly 6-10 cards) and avoid unbounded fan-out until the pattern is proven.

**Constraints**: Fable 5 is unavailable/banned; no production side effects without Jared approval; use actual configured profiles; durable workspaces only for downstream artifacts; avoid a plugin until the Kanban-native pattern proves value.

**Scale/Scope**: First dogfood workflow plus reusable pattern; not a complete replacement for Claude Code Dynamic Workflows or a new generalized orchestration engine.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- Durable state before autonomous action: PASS — specs and Kanban are primary artifacts.
- Evidence-gated execution: PASS — each card must define evidence requirements.
- Human approval for external side effects: PASS — merge/deploy/external action remains Jared-gated.
- Profile/workspace isolation: PASS — assignee discovery completed and cards must use durable workspaces/handoffs.
- Skills capture proven workflows: PASS — skill promotion is part of closeout.

## Project Structure

### Documentation (this feature)

```text
specs/001-fable-emulation-workflow/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── kanban-card-contract.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
.specify/
├── memory/constitution.md
└── feature.json

/home/red/Documents/Obsidian Vault/Research/
├── Claude Fable 5 - Why It Feels Amazing and How It Works.md
└── Fable 5 Agentic Workflow HTML Design and Flowchart.md

/home/red/.hermes/status-page/public/
├── fable5-overview.html
└── fable5-agentic-workflow-design.html

/home/red/.hermes/skills/
└── existing skills to patch or new fable-style workflow skill after dogfood
```

**Structure Decision**: Keep Spec Kit artifacts in the Hermes Agent repo because the workflow affects Hermes-native orchestration. Keep research artifacts in Obsidian/Tailnet. Groom Kanban against a dedicated board so execution state does not pollute unrelated project boards.

## Complexity Tracking

No constitution violations are expected. A plugin is intentionally deferred; the simpler Kanban-native path is being chosen first.
