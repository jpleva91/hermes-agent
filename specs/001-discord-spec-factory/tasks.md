# Tasks: Hermes-Native Discord-to-Spec-Kit Software Factory

**Input**: Design documents from `/specs/001-discord-spec-factory/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Tests are required for this feature because acceptance criteria explicitly require local validation of config, source packets, thread semantics, permissions, role routing, Kanban projection, and GitHub handoff.

**Organization**: Tasks are grouped by user story to enable independently testable increments.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Include exact file paths in descriptions

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the Spec Kit pack and local steel-thread surface.

- [x] T001 Create Spec Kit feature directory `specs/001-discord-spec-factory/`
- [x] T002 [P] Write feature specification in `specs/001-discord-spec-factory/spec.md`
- [x] T003 [P] Write research decisions in `specs/001-discord-spec-factory/research.md`
- [x] T004 [P] Write implementation plan in `specs/001-discord-spec-factory/plan.md`
- [x] T005 [P] Write data model in `specs/001-discord-spec-factory/data-model.md`
- [x] T006 [P] Write quickstart in `specs/001-discord-spec-factory/quickstart.md`
- [x] T007 [P] Write governance policy in `specs/001-discord-spec-factory/governance.yaml`
- [x] T008 [P] Write interface contracts under `specs/001-discord-spec-factory/contracts/`
- [x] T009 [P] Write requirements checklist in `specs/001-discord-spec-factory/checklists/requirements.md`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Pure local helpers that do not touch live gateway/config/credentials.

- [x] T010 Create pure helper module `hermes_cli/discord_spec_factory.py`
- [x] T011 Create local unit tests `tests/hermes_cli/test_discord_spec_factory.py`
- [x] T012 [P] Implement fail-closed intake binding validation in `hermes_cli/discord_spec_factory.py`
- [x] T013 [P] Implement deterministic workflow/thread ID and source packet construction in `hermes_cli/discord_spec_factory.py`
- [x] T014 [P] Implement role route validation in `hermes_cli/discord_spec_factory.py`
- [x] T015 Run focused pytest for `tests/hermes_cli/test_discord_spec_factory.py`

**Checkpoint**: Foundation ready - gateway integration can now be planned without live side effects.

---

## Phase 3: User Story 1 - Intake creates a local Spec Kit seed (Priority: P1) 🎯 MVP

**Goal**: A Discord intake request can be validated and transformed into a canonical source/context packet for Spec Kit.

**Independent Test**: Synthetic Discord-like dict produces one deterministic packet and placeholder channel config fails closed.

### Tests for User Story 1

- [x] T016 [P] [US1] Add placeholder channel fail-closed test in `tests/hermes_cli/test_discord_spec_factory.py`
- [x] T017 [P] [US1] Add source packet hashing/dedupe identity test in `tests/hermes_cli/test_discord_spec_factory.py`
- [x] T018 [US1] Add local Spec Kit seed initialization, repository confinement, and immutable source-packet tests in `tests/hermes_cli/test_discord_spec_factory.py`

### Implementation for User Story 1

- [x] T019 [US1] Implement `validate_intake_binding()` in `hermes_cli/discord_spec_factory.py`
- [x] T020 [US1] Implement `build_source_context_packet()` in `hermes_cli/discord_spec_factory.py`
- [x] T021 [US1] Implement `workflow_thread_name()` in `hermes_cli/discord_spec_factory.py`
- [x] T022a [US1] Implement synthetic local intake helper `process_synthetic_discord_intake()` in `hermes_cli/discord_spec_factory.py`
- [x] T022b [US1] Integrate packet creation with a disabled-by-default gateway intake/thread seam in `gateway/run.py`
- [ ] T022c [US1] Wire official repository-local `.specify` workflow/script execution behind a safe non-network/process-risk integration, or explicitly keep using `initialize_local_spec_kit_seed()` until that exists

**Checkpoint**: User Story 1 local steel thread is functional without Discord network access.

---

## Phase 4: User Story 2 - Clarify, review, and approve asynchronously in Discord threads (Priority: P2)

**Goal**: Workflow gates can be resolved from dedicated Discord thread replies and persisted to Spec Kit state.

**Independent Test**: A synthetic thread reply from an authorized actor advances a pending gate; unauthorized reply does not.

### Tests for User Story 2

- [x] T023 [P] [US2] Add workflow thread mapping tests in `tests/gateway/test_discord_spec_factory_threads.py`
- [x] T024 [P] [US2] Add approval gate authorization tests in `tests/hermes_cli/test_discord_spec_factory.py`

### Implementation for User Story 2

- [x] T025 [US2] Add workflow thread registry functions in `hermes_cli/discord_spec_factory.py`
- [x] T026 [US2] Add approval gate decision functions in `hermes_cli/discord_spec_factory.py`
- [x] T027 [US2] Wire Discord thread replies to clarify/approval gate decisions through existing gateway event handling in `gateway/run.py`

**Checkpoint**: Human gates advance only through authorized workflow-thread replies.

---

## Phase 5: User Story 3 - Execute with role/profile/model routing (Priority: P3)

**Goal**: Execution phases route to existing Hermes profiles and model tiers through explicit contracts.

**Independent Test**: Complete route map passes; missing route blocks execution with a recoverable reason.

### Tests for User Story 3

- [x] T028 [P] [US3] Add required role route validation tests in `tests/hermes_cli/test_discord_spec_factory.py`
- [x] T029 [P] [US3] Add profile existence dry-run tests in `tests/hermes_cli/test_discord_spec_factory.py`

### Implementation for User Story 3

- [x] T030 [US3] Implement `validate_role_routes()` in `hermes_cli/discord_spec_factory.py`
- [x] T031 [US3] Implement execution packet construction in `hermes_cli/discord_spec_factory.py` (packet-only; no gateway dispatch)
- [ ] T032 [US3] Integrate profile dispatch only after approval gates in a future dispatcher module

**Checkpoint**: Route validation is fail-closed before any worker process can spawn.

---

## Phase 6: User Story 4 - Project optionally to Kanban and hand off to GitHub (Priority: P4)

**Goal**: Kanban mirrors and GitHub handoff are optional projections from canonical Spec Kit state.

**Independent Test**: Disabling Kanban leaves workflow state unchanged; PR target contract validates locally without remote git.

### Tests for User Story 4

- [x] T033 [P] [US4] Add Kanban projection disabled-mode tests in `tests/hermes_cli/test_discord_spec_factory.py`
- [x] T034 [P] [US4] Add PR target validation tests in `tests/hermes_cli/test_discord_spec_factory.py`

### Implementation for User Story 4

- [x] T035 [US4] Implement Kanban projection packet builder in `hermes_cli/discord_spec_factory.py` (packet-only; no gateway mutation)
- [x] T036 [US4] Implement PR target validation in `hermes_cli/discord_spec_factory.py` (packet-only; remote effects disabled)
- [ ] T037 [US4] Wire approved GitHub handoff to existing CLI/MCP surfaces without remote side effects in tests

**Checkpoint**: Projection/handoff contracts are validated and remain derived from Spec Kit.

---

## Final Phase: Polish & Cross-Cutting Concerns

- [x] T038 [P] Add user documentation for enabling the Discord Spec Factory in `website/docs/` after code integration is complete
- [x] T039 [P] Add migration/rollback operator checklist to `website/docs/` or `docs/`
- [x] T040a Run focused local helper and synthetic thread tests for changed paths
- [x] T040b Run focused gateway/platform tests for disabled-by-default intake/thread-gate paths
- [x] T041 Run broader relevant pytest selection before PR handoff
- [ ] T042 Prepare PR body with source PDF summary, Spec Kit artifacts, tests, and rollback plan

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: depends on Setup artifacts.
- **US1**: depends on Foundational helpers.
- **US2**: depends on US1 workflow/thread identity.
- **US3**: depends on route validation from Foundational and gates from US2 before live dispatch.
- **US4**: depends on canonical workflow state and approval gates.

### User Story Dependencies

- **US1 (P1)**: MVP; can be demonstrated locally now.
- **US2 (P2)**: builds on US1 thread/workflow IDs.
- **US3 (P3)**: can validate routes independently, but live dispatch must wait for US2 gates.
- **US4 (P4)**: projection/handoff can be developed after canonical state is stable.

### Parallel Opportunities

- Documentation contracts can be reviewed in parallel.
- Route validation and source packet tests are independent.
- Kanban projection and PR target validators can be developed in parallel once base packet/workflow IDs exist.

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Spec Kit pack.
2. Add pure helper module and tests.
3. Validate placeholder intake fails closed.
4. Validate synthetic packet formation.
5. Stop before live gateway integration.

### Incremental Delivery

1. Local helper + contract tests.
2. Synthetic gateway seam.
3. Thread/gate persistence.
4. Profile routing dispatch.
5. Optional Kanban and GitHub handoff.

## Notes

- Do not commit, push, PR, restart gateway, edit live config, or touch credentials in this worktree task.
- Every future gateway integration must preserve prompt caching and role alternation.
- Kanban is never canonical; treat it as notification/projection only.
