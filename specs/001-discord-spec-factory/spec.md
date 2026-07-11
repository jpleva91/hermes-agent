# Feature Specification: Hermes-Native Discord-to-Spec-Kit Software Factory

**Feature Branch**: `feature/discord-spec-factory-v1`

**Created**: 2026-07-10

**Status**: Draft

**Input**: User description: "Build the PDF idea into Hermes itself: Discord intake channel -> Hermes triage -> canonical GitHub Spec Kit persisted workflow state -> async clarify/review/approval in dedicated Discord threads -> role/model-routed execution across existing Hermes profiles -> optional Kanban projection/notifications -> GitHub handoff. Kanban is not source of truth."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Intake creates a local Spec Kit seed (Priority: P1)

A maintainer posts a natural-language request, attachment, or voice note into a configured Discord intake channel and Hermes converts it into a repository-local Spec Kit seed, with a traceable source/context packet and a dedicated Discord workflow thread. Official Spec Kit workflow execution remains future work until the repository-local `.specify` workflow/scripts are invoked through a safe in-process integration or a separately approved dispatcher.

**Why this priority**: This is the factory steel thread. Without canonical Spec Kit state and thread isolation, later execution is just chat automation.

**Independent Test**: Configure a placeholder Discord channel ID in a temp Hermes home, feed a synthetic Discord message with text and one attachment into the intake helper, and verify that a repository-local Spec Kit seed directory/workflow-state helper is created, a thread-open request is emitted, duplicate packets resume without overwrite, edited source content is recorded as an observation, and no Kanban card is required for canonical progress.

**Acceptance Scenarios**:

1. **Given** Discord intake is enabled for channel `DISCORD_SPEC_FACTORY_INTAKE_CHANNEL_ID`, **When** an authorized user posts a request, **Then** Hermes records the Discord source packet, initializes a repository-local Spec Kit seed, and prepares a reply only for a dedicated workflow thread.
2. **Given** the intake message contains supported attachments or a Discord voice note, **When** Hermes triages the request, **Then** the source packet references cached attachment paths and available transcripts without embedding secrets or uncontrolled binary content into the spec.
3. **Given** Spec Kit initialization fails, **When** the intake message is processed, **Then** Hermes posts a fail-closed error to the source thread/channel and does not create Kanban tasks or spawn execution profiles.

---

### User Story 2 - Clarify, review, and approve asynchronously in Discord threads (Priority: P2)

A maintainer can resolve Spec Kit clarify/review/approval gates inside the dedicated workflow thread, while Hermes persists every decision in Spec Kit workflow state and keeps the thread as the human-facing conversation surface.

**Why this priority**: Spec-driven development depends on durable approval gates; Discord is only the UI, not the state machine.

**Independent Test**: Start from a persisted workflow requiring clarify approval, send synthetic thread replies, and verify that the state advances only after explicit authorization and stores the decision with actor, timestamp, and source message ID.

**Acceptance Scenarios**:

1. **Given** a workflow asks a clarification question, **When** an authorized user replies in the dedicated Discord thread, **Then** Hermes appends the answer to the Spec Kit state and advances the clarify step.
2. **Given** a workflow reaches a review gate, **When** a user without approval rights replies, **Then** Hermes records the attempt as denied/ignored and leaves the workflow blocked.
3. **Given** the Discord thread is unavailable, **When** a gate is reached, **Then** Hermes keeps the workflow blocked and surfaces a recoverable notification target rather than silently proceeding.

---

### User Story 3 - Execute with role/profile/model routing (Priority: P3)

After approvals, Hermes routes each workflow phase to existing Hermes profiles and model tiers according to an explicit routing contract, with bounded permissions and auditable handoff packets.

**Why this priority**: Existing Hermes profiles are the worker pool; the factory must route by role without making Kanban lanes the source of truth.

**Independent Test**: Given a workflow plan with roles `spec-lead`, `architect`, `implementer`, `reviewer`, and `release-captain`, verify the router selects configured profiles/model tiers, denies missing required routes, and produces a deterministic execution packet for each phase.

**Acceptance Scenarios**:

1. **Given** all required routes exist, **When** the workflow enters implementation, **Then** Hermes dispatches the phase to the configured profile/model with the source/context packet, current Spec Kit artifact paths, and PR target contract.
2. **Given** a required route is missing or disabled, **When** Hermes attempts dispatch, **Then** it fails closed, records the missing route, and asks for human repair in the workflow thread.
3. **Given** a worker proposes destructive operations or PR creation, **When** the action crosses the configured permission boundary, **Then** Hermes requires the relevant approval gate before proceeding.

---

### User Story 4 - Project optionally to Kanban and hand off to GitHub (Priority: P4)

A maintainer can opt into Kanban projection and GitHub handoff without making either one replace Spec Kit as canonical workflow state.

**Why this priority**: Kanban and GitHub are valuable operator surfaces, but the corrected scope explicitly says Kanban is not the source of truth.

**Independent Test**: Enable Kanban projection for a workflow, complete a phase, and verify that Kanban cards mirror Spec Kit phase status while the canonical state remains in Spec Kit artifacts; then verify the GitHub handoff packet targets the configured base branch and repository.

**Acceptance Scenarios**:

1. **Given** Kanban projection is disabled, **When** a workflow advances, **Then** no Kanban cards are required and Spec Kit state still advances.
2. **Given** Kanban projection is enabled, **When** a phase starts or completes, **Then** Hermes updates mirror cards with links back to the Spec Kit feature directory and Discord thread.
3. **Given** a workflow is ready for handoff, **When** approval is granted, **Then** Hermes prepares a GitHub handoff with target repository, base branch, branch naming, PR title/body, test evidence, and rollback notes.

### Edge Cases

- Intake posted in a configured parent channel but inside an existing unrelated thread: inherit channel policy, but create/reuse only a workflow-owned thread identified by workflow ID.
- Duplicate Discord delivery or retry of the same message: dedupe by platform, channel ID, message ID, and attachment hashes before creating workflow state.
- Unsupported attachment type or oversized file: retain metadata and a user-visible warning; do not inline or execute content.
- Voice transcription unavailable: keep audio attachment reference, mark transcript absent, and ask a clarification if text is insufficient.
- User deletes or edits the source Discord message: preserve the captured source packet and append a later observation; never mutate past canonical state silently.
- Gateway restarts mid-workflow: recover from Spec Kit state and session/thread index, not from in-memory queues.
- Kanban projection failure: leave Spec Kit state untouched and surface projection error as non-blocking unless policy says otherwise.
- GitHub credentials missing or repository not configured: block only handoff, not earlier specification/clarification work.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Hermes MUST support a Discord intake channel configuration with an explicit placeholder/default-denied channel ID and an enable flag; production intake MUST NOT activate on an empty or wildcard channel by default.
- **FR-002**: Hermes MUST use existing Discord `channel_prompts`, `channel_skill_bindings`, and `create_handoff_thread` surfaces where possible before adding new gateway primitives.
- **FR-003**: Hermes MUST create a repository-local Spec Kit seed and workflow-state helper for every accepted intake request before any implementation worker is spawned; official Spec Kit workflow execution MUST remain unchecked until wired to repository-local `.specify` workflow/scripts without network/process side effects in tests.
- **FR-004**: Hermes MUST persist a source/context packet containing Discord channel ID, thread ID if present, message ID, author identity reference, timestamp, text, attachment metadata, voice transcript metadata, repository context, and workflow ID.
- **FR-005**: Hermes MUST create or reuse a dedicated Discord workflow thread for clarification, review, approval, and progress notifications; parent intake channel chatter MUST NOT be the long-running workflow surface.
- **FR-006**: Hermes MUST support async clarify gates in the workflow thread and persist answers back to Spec Kit state with actor and Discord message provenance.
- **FR-007**: Hermes MUST support review/approval gates that fail closed when the actor lacks required permissions, when the route is ambiguous, or when Discord/thread delivery fails.
- **FR-008**: Hermes MUST define a role/profile/model routing contract for at least `intake-triage`, `spec-lead`, `architect`, `implementer`, `reviewer`, `release-captain`, and `operator-notifier` roles.
- **FR-009**: Hermes MUST route execution across existing Hermes profiles by configured role, profile name, toolset allowance, model/provider/tier, workspace boundary, and approval requirements.
- **FR-010**: Hermes MUST keep Kanban projection optional and derived; Kanban card status MUST NOT be treated as canonical workflow state for Spec Kit progress.
- **FR-011**: Hermes MUST define a PR target contract including repository, base branch, feature branch naming, commit policy, PR title/body template, required evidence, and rollback instructions.
- **FR-012**: Hermes MUST handle text, Discord attachments, and voice notes; attachments MUST be cached/referenced through existing safe media handling and voice notes MUST use existing transcription behavior when available.
- **FR-013**: Hermes MUST deduplicate repeated Discord events so one source message cannot create multiple canonical workflows unless a maintainer explicitly forks it.
- **FR-014**: Hermes MUST record every state transition with workflow ID, step, actor/system source, timestamp, and evidence pointers.
- **FR-015**: Hermes MUST provide migration and rollback instructions for enabling, disabling, and recovering the factory without touching live gateway config or credentials during development.
- **FR-016**: Hermes MUST include acceptance tests for config validation, source packet creation, thread semantics, permission failures, role routing, optional Kanban projection, and GitHub handoff packet formation.

### Key Entities *(include if feature involves data)*

- **Factory Intake Binding**: A configured Discord channel/forum ID, required roles/users, channel prompt, skill binding, enablement flag, and default fail-closed policy.
- **Source Context Packet**: Immutable captured provenance for a request, including Discord source, author reference, text, attachments, voice transcript state, repository context, and derived workflow ID.
- **Spec Kit Workflow State**: Repository-local seed feature directory plus helper state for specify, clarify, plan, tasks, implement, converge, review, and handoff phases. This v1 steel thread does not execute the official Spec Kit state engine yet.
- **Workflow Thread**: Dedicated Discord thread linked to exactly one workflow ID and used for async user interaction.
- **Role Route**: Mapping from factory role to Hermes profile, model/provider/tier, toolsets, permissions, workspace, and escalation behavior.
- **Approval Gate**: A persisted decision point requiring a specific actor/role before state transition or side effect.
- **Kanban Projection**: Optional mirror of workflow phases/tasks into Kanban cards, linked back to canonical state but never authoritative.
- **GitHub Handoff**: Final handoff contract for repository, branch, PR metadata, evidence, and rollback.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of accepted Discord intake messages produce exactly one canonical Spec Kit workflow ID in repeat/deduplication tests.
- **SC-002**: 100% of approval-gated transitions remain blocked when the actor lacks permission or the route is missing.
- **SC-003**: A maintainer can trace any generated PR handoff back to the original Discord message, source packet, Spec Kit feature directory, and approval messages in under 2 minutes.
- **SC-004**: Kanban projection can be disabled with zero failing core workflow tests and no loss of Spec Kit state advancement.
- **SC-005**: Synthetic gateway restart tests recover workflow/thread association from persisted state without relying on in-memory objects.
- **SC-006**: The MVP steel thread runs locally without live Discord, GitHub, gateway restart, remote git, or credentials by using synthetic events and temp Hermes homes.

## Assumptions

- The v1 implementation runs inside Hermes Agent and uses the existing Discord gateway adapter rather than a separate service.
- Official Spec Kit 0.12.10 with `integration=hermes` is the canonical workflow engine for this worktree.
- Existing Hermes profiles are pre-created by the operator; this feature validates and routes to them but does not create the user's 21 live profiles.
- The source of truth is repository-local Spec Kit artifacts/state under the repository; Discord threads, Kanban, and GitHub are projections or interfaces. Official Spec Kit workflow execution remains a future unchecked integration task.
- Development and tests use isolated temp homes/configs and synthetic Discord events; no live config, credentials, gateway restart, remote git, or live profile mutation is allowed.
- The initial placeholder channel ID is `DISCORD_SPEC_FACTORY_INTAKE_CHANNEL_ID` and must be replaced by operators before live enablement.
