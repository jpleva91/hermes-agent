# Feature Specification: Fable Emulation Workflow

**Feature Branch**: `001-fable-emulation-workflow`

**Created**: 2026-06-13

**Status**: Draft

**Input**: User description: "Specify a Hermes-native workflow that emulates banned/unavailable Claude Fable 5 behavior using Hermes as mission control, GPT-5.5/Codex and available Claude Code-style workers as implementation lanes, NotebookLM for source-grounded synthesis, Kanban as the durable execution ledger, skills as procedural memory, cron for scheduled triggers, worktrees for isolation, and fresh-context verification gates before Jared approves merges/deploys/external side effects."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Capture a Fable-like work request as a durable spec-to-board workflow (Priority: P1)

Jared can give Hermes a broad, high-agency work request and receive a durable Spec Kit-backed plan that decomposes the work into independently testable lanes before any coding worker starts.

**Why this priority**: This is the minimum viable emulation layer. Without a spec-to-board intake, the workflow becomes scattered chat and cannot reproduce the sustained planning behavior Jared liked.

**Independent Test**: Can be tested by giving a broad workflow request and verifying that Hermes produces a Spec Kit spec/plan/tasks packet plus a Kanban-ready task graph with no implementation cards dispatched before the spec exists.

**Acceptance Scenarios**:

1. **Given** a broad Fable-emulation request, **When** Hermes runs the intake, **Then** it creates or updates Spec Kit artifacts with user stories, requirements, success criteria, plan, research notes, and task slices.
2. **Given** the Spec Kit artifacts exist, **When** Hermes prepares Kanban grooming, **Then** each candidate card names owner lane, durable workspace, dependencies, acceptance criteria, and evidence requirements.
3. **Given** ambiguity affects scope, safety, or ownership, **When** Hermes cannot make a safe default, **Then** the spec records at most three explicit clarification markers or blocks the relevant Kanban card instead of guessing.

---

### User Story 2 - Execute work through Kanban lanes that emulate dynamic fan-out/fan-in (Priority: P2)

Jared can route work through Hermes Kanban so research, planning, implementation, and review lanes run independently where possible and converge into a synthesis/review gate before final approval.

**Why this priority**: Fable-like behavior depends on harness-native orchestration: fan-out, isolated contexts, state handoff, and fan-in. Kanban is the durable execution backbone for that behavior.

**Independent Test**: Can be tested by grooming a board from this spec and verifying that independent research/discovery cards have no unnecessary parent links, dependent implementation/review cards are correctly gated, and the dispatcher can see actual configured assignees.

**Acceptance Scenarios**:

1. **Given** multiple independent lanes, **When** Hermes grooms Kanban, **Then** independent cards are ready in parallel and synthesis/review cards depend on their parent outputs.
2. **Given** a card requires generated artifacts, **When** the card is created, **Then** its workspace is durable (`dir:` or worktree) or its expected handoff content is captured in comments/results.
3. **Given** an assignee is unknown or a poller identity is ambiguous, **When** Hermes grooms the card, **Then** the card remains blocked/triaged or uses a verified profile instead of silently assigning to a nonexistent worker.

---

### User Story 3 - Verify outputs with clean-context review before Jared approval (Priority: P3)

Jared receives evidence-backed results that include independent verification, not just a worker's claim that the task is complete.

**Why this priority**: The emulation is unsafe without verification. Fresh-context review and deterministic gates are the difference between autonomous-feeling work and unreliable automation.

**Independent Test**: Can be tested by completing a small code or docs change through the workflow and confirming the final handoff includes source artifacts, test/log evidence, review verdict, caveats, and explicit approval state.

**Acceptance Scenarios**:

1. **Given** a code-changing card completes implementation, **When** it reaches review, **Then** a fresh reviewer inspects diff, tests, logs, and acceptance criteria before the card is considered complete.
2. **Given** verification fails, **When** the reviewer reports blockers, **Then** Hermes creates or updates follow-up remediation cards instead of marking the original work done.
3. **Given** the result implies merge/deploy/external action, **When** all automated checks pass, **Then** Hermes still asks Jared for explicit approval unless the card explicitly granted that side effect.

---

### User Story 4 - Promote repeated success into reusable skills and reporting (Priority: P4)

After the workflow succeeds, Hermes captures the procedure as a skill or skill patch, updates Obsidian/Tailnet reporting, and uses the learned process for future requests.

**Why this priority**: Fable-like persistence must improve over time. The reusable part in Hermes is skills, specs, board patterns, and reports rather than hidden model state.

**Independent Test**: Can be tested by completing a workflow and verifying that a skill/pattern update is proposed or written, Obsidian/Tailnet references are updated, and future cards can point to the saved procedure.

**Acceptance Scenarios**:

1. **Given** a workflow completes and required more than a trivial path, **When** Hermes summarizes the outcome, **Then** it identifies whether to create or patch a skill.
2. **Given** durable artifacts were produced, **When** the workflow closes, **Then** Obsidian and/or Tailnet links point to the spec, board, and evidence bundle.

### Edge Cases

- Fable 5 remains unavailable or banned: the workflow must not depend on direct Fable access.
- GPT-5.5/Codex quota or latency makes a lane impractical: the workflow must support rerouting or blocking with a cost/availability reason.
- Kanban profile exists but is not the intended external poller: cards must document poller identity instead of assuming profile existence means auto-spawn is allowed.
- Scratch workspace produces useful files: the card must copy them to durable storage or embed them in comments/results before completion.
- Dynamic fan-out would create too many cards: Hermes should cap initial grooming to a reviewable MVP graph and leave expansion cards gated.
- Research sources conflict: NotebookLM/Obsidian synthesis must label confidence and caveats.
- Reviewer finds implementation drift from spec: create remediation cards and keep merge blocked.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST treat Fable 5 as a banned/unavailable behavior reference target, not as a required execution dependency.
- **FR-002**: The system MUST create Spec Kit artifacts before grooming implementation cards for policy-heavy, cross-agent, or workflow-level changes.
- **FR-003**: The system MUST convert accepted Spec Kit tasks into a Kanban task graph with explicit dependencies, owners, workspaces, acceptance criteria, and evidence requirements.
- **FR-004**: The system MUST discover or verify available Kanban assignees before assigning cards.
- **FR-005**: The system MUST keep independent lanes parallel and gate synthesis/review/implementation only on true dependencies.
- **FR-006**: The system MUST require durable output handoffs for any card whose artifacts are needed by downstream cards.
- **FR-007**: The system MUST include fresh-context review or deterministic verification before code-changing work is considered complete.
- **FR-008**: The system MUST preserve Jared as approval gate for merge, deploy, production, account, public posting, or external side effects unless explicitly authorized.
- **FR-009**: The system MUST record model-routing guidance and cost/availability caveats on relevant implementation or review cards.
- **FR-010**: The system MUST update durable reporting surfaces (Obsidian and/or Tailnet) when the spec/board/workflow reaches meaningful milestones.
- **FR-011**: The system MUST identify reusable workflow learnings and create or patch skills after non-trivial successful execution.
- **FR-012**: Users MUST be able to inspect the current spec path, board name, card graph, and evidence bundle from the final handoff.

### Key Entities *(include if feature involves data)*

- **Workflow Spec**: Spec Kit feature directory containing spec.md, plan.md, research.md, data-model.md, quickstart.md, contracts, and tasks.md.
- **Kanban Board**: Durable task queue used as execution ledger for the workflow.
- **Kanban Card**: Work unit with title, assignee, status, body, parents, workspace, acceptance criteria, and evidence requirements.
- **Worker Lane**: A configured Hermes profile or external poller identity responsible for a class of tasks.
- **Evidence Bundle**: Verifiable completion packet containing artifacts, source citations, commands, tests/logs, diffs, screenshots, task IDs, and caveats.
- **Skill Promotion Candidate**: A repeated or non-trivial procedure that should become a Hermes skill or skill patch.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A broad workflow request can be converted into Spec Kit artifacts and a Kanban-ready task graph in one planning pass without implementation workers starting prematurely.
- **SC-002**: 100% of groomed cards identify owner lane, durable workspace or durable handoff, dependency state, acceptance criteria, and verification evidence.
- **SC-003**: 100% of code-changing cards include a review gate before merge/deploy/external action.
- **SC-004**: At least one independent fan-out/fan-in workflow can run through Kanban with parent dependency promotion and a final synthesis/review card.
- **SC-005**: Final user handoff includes concrete handles: spec path, board name, card IDs, artifact paths/URLs, and approval/blocker status.
- **SC-006**: The first completed dogfood workflow produces either a new skill, a patch to an existing skill, or an explicit documented reason no skill promotion is needed.

## Assumptions

- The durable repository for this spec is `/home/red/.hermes/hermes-agent`.
- Initial Kanban grooming should use actual available profiles: `posresearch`, `possynthesis`, `poscoding`, `posreview`, `posops`, `default`, and `readybench` only when appropriate.
- The initial board can be created specifically for this workstream unless Jared directs use of an existing board.
- GPT-5.5/Codex is available as the primary strong reasoning/worker lane; Fable 5 is not assumed available.
- NotebookLM and Obsidian remain research/synthesis surfaces, not the canonical task queue.
