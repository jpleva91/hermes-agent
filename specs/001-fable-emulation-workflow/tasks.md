# Tasks: Fable Emulation Workflow

**Input**: Design documents from `/specs/001-fable-emulation-workflow/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/kanban-card-contract.md

**Tests**: Include validation/verification tasks because this workflow is safety- and routing-sensitive.

**Organization**: Tasks are grouped by user story to enable independent implementation and Kanban grooming.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Include exact file paths or durable artifact paths in descriptions

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish durable Spec Kit and reporting artifacts before board execution.

- [x] T001 [US1] Initialize Spec Kit in `/home/red/.hermes/hermes-agent` with Claude integration.
- [x] T002 [US1] Write constitution in `.specify/memory/constitution.md`.
- [x] T003 [US1] Create feature spec directory `specs/001-fable-emulation-workflow/`.
- [x] T004 [US1] Write `spec.md`, `plan.md`, `research.md`, `data-model.md`, `quickstart.md`, `contracts/kanban-card-contract.md`, and `checklists/requirements.md`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Verify live routing surfaces before implementation cards are created.

- [ ] T005 [P] [US2] Verify Kanban board creation/listing for `fable-emulation-workflow` using `hermes kanban` CLI.
- [ ] T006 [P] [US2] Verify actual assignees from `hermes profile list` and document which lanes are allowed to auto-spawn.
- [ ] T007 [US2] Create root SPEC REQUEST card on the selected board with links to this Spec Kit directory, Obsidian notes, and Tailnet artifact.
- [ ] T008 [US2] Create board preflight note/comment that records owner, workspace, verification, and auto-spawn policy for each lane.

**Checkpoint**: Board exists, assignees are known, and root card points to durable specs.

---

## Phase 3: User Story 1 - Spec-to-board intake (Priority: P1) 🎯 MVP

**Goal**: Convert the spec into a Kanban-ready task graph without starting implementation too early.

**Independent Test**: Inspect the board and verify all initial cards reference `specs/001-fable-emulation-workflow/` and include acceptance criteria/evidence requirements.

- [ ] T009 [US1] Create research card for validating current Hermes Kanban docs/capabilities; assign to `posresearch`; workspace no-files-needed; evidence: sources/commands/summary.
- [ ] T010 [US1] Create discovery card for inspecting existing Kanban CLI/plugin code paths relevant to preflight/dispatch; assign to `poscoding` or `posresearch` based on worker identity; durable workspace `dir:/home/red/.hermes/hermes-agent` if code artifacts are needed.
- [ ] T011 [US1] Create synthesis card depending on T009 and T010; assign to `possynthesis`; evidence: recommended board operating pattern and gaps.

**Checkpoint**: MVP spec-to-board graph is visible and dependency-linked.

---

## Phase 4: User Story 2 - Dynamic fan-out/fan-in execution (Priority: P2)

**Goal**: Dogfood one real workflow through Kanban as the Fable-emulation reference run.

**Independent Test**: Independent lanes run or are ready in parallel; synthesis/review waits on parents.

- [ ] T012 [US2] Create dogfood-run planning card depending on T011; assign to `possynthesis`; evidence: chosen first workflow, scope, card graph, and stop conditions.
- [ ] T013 [P] [US2] Create implementation-lane card for any needed docs/CLI/status-page update; assign to verified coding lane; workspace must be durable repo/worktree.
- [ ] T014 [P] [US2] Create ops-lane card for dispatcher/profile/preflight verification; assign to `posops`; evidence: commands, status, known blockers.
- [ ] T015 [US2] Create fan-in synthesis card depending on T013 and T014; assign to `possynthesis`; evidence: what worked, what failed, recommended next iteration.

**Checkpoint**: One fan-out/fan-in dogfood loop has completed or produced explicit blockers.

---

## Phase 5: User Story 3 - Fresh-context verification (Priority: P3)

**Goal**: Ensure outputs are reviewed before Jared approval.

**Independent Test**: A review card blocks or approves based on evidence, not worker claims.

- [ ] T016 [US3] Create review card depending on T015; assign to `posreview`; evidence: spec compliance verdict, code/docs quality verdict, and explicit approval/blocker state.
- [ ] T017 [US3] If review blocks, create remediation card assigned to original owner with parent link to review card; evidence: fixes and rerun checks.
- [ ] T018 [US3] If review passes, prepare Jared approval handoff with spec path, board, card IDs, artifacts, checks, and side-effect status.

**Checkpoint**: No merge/deploy/external action happens without Jared approval.

---

## Phase 6: User Story 4 - Skill/report promotion (Priority: P4)

**Goal**: Capture the proven workflow so it compounds.

**Independent Test**: Obsidian/Tailnet/skill artifacts link to the board and spec.

- [ ] T019 [US4] Update Obsidian research note with final spec path, board name, card graph, and evidence bundle.
- [ ] T020 [US4] Update Tailnet design/overview page with current state and board link/handles.
- [ ] T021 [US4] Create or patch a Hermes skill for Fable-style emulation workflow if the dogfood run proves reusable.
- [ ] T022 [US4] Final closeout: report completed cards, blocked cards, evidence, and recommended next spec/plugin decision.

---

## Dependencies & Execution Order

### Phase Dependencies

- Phase 1 is complete.
- Phase 2 blocks all Kanban execution.
- Phase 3 depends on Phase 2.
- Phase 4 depends on Phase 3 synthesis.
- Phase 5 depends on Phase 4 synthesis.
- Phase 6 depends on review/approval state from Phase 5.

### Parallel Opportunities

- T005 and T006 can run in parallel.
- T009 and T010 can run in parallel after root card/preflight exists.
- T013 and T014 can run in parallel after dogfood-run planning.
- T019 and T020 can run in parallel after final evidence bundle exists.

## Implementation Strategy

### MVP First

1. Complete Phase 2 board/profile preflight.
2. Create root card and Phase 3 research/discovery/synthesis cards.
3. Stop and inspect board graph before dispatching deeper implementation.

### Dogfood Then Plugin

1. Prove the workflow manually through Kanban.
2. Identify repetitive enforcement needs.
3. Only then specify a plugin or CLI enhancement.

## Notes

- Do not assign to invented profiles.
- Do not use scratch workspaces for durable artifacts.
- Do not imply Jared approval for side effects.
- Do not treat Fable 5 as available.
