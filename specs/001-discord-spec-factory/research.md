# Research: Hermes-Native Discord-to-Spec-Kit Software Factory

## Decision: Spec Kit workflow state is canonical

**Rationale**: The corrected scope explicitly says the PDF idea must be built into Hermes itself with canonical GitHub Spec Kit persisted workflow state. Spec Kit already provides the specify → clarify → plan → tasks → implement flow and has been initialized in this worktree as `integration=hermes` version 0.12.10. Treating Spec Kit as the source of truth preserves traceability from Discord request to artifacts and PR handoff.

**Alternatives considered**:
- Kanban as canonical state: rejected because the user explicitly corrected that Kanban must not be source of truth.
- Discord thread as canonical state: rejected because threads are mutable/external UI and insufficient for repo-portable workflow state.
- GitHub issues as canonical state: rejected for v1 because credentials/repository handoff may be absent while specification should still proceed locally.

## Decision: Discord dedicated workflow threads are the async human interface

**Rationale**: Hermes already has `create_handoff_thread` and Discord thread support. A dedicated thread isolates clarification/review/approval from intake channel chatter and creates a human-readable audit trail. Thread IDs are stored as projections linked to workflow IDs.

**Alternatives considered**:
- Continue in the intake channel: rejected because multiple workflows would interleave and prompt/context boundaries would be unclear.
- DM-only approvals: rejected as the primary path because source teams need shared visibility; may be a fallback notification target later.

## Decision: Use existing channel prompts and skill bindings for intake steering

**Rationale**: Existing Discord config supports `channel_prompts`, `channel_skill_bindings`, and parent/thread inheritance. Reusing these surfaces keeps the core narrow and avoids a new model-tool or gateway primitive for early versions.

**Alternatives considered**:
- New always-on Discord factory toolset: rejected because it increases model schema footprint and duplicates existing gateway dispatch.
- External bot/service: rejected because the target is Hermes-native.

## Decision: Role/profile/model routing is explicit, required, and fail-closed

**Rationale**: The factory relies on existing Hermes profiles. A declarative role route lets operators map `spec-lead`, `architect`, `implementer`, `reviewer`, `release-captain`, and notifier roles to profile names, model/provider/tier choices, toolset limits, and approval boundaries. Missing routes must block execution rather than silently falling back to a powerful default profile.

**Alternatives considered**:
- Infer profile from task title: rejected for safety and reproducibility.
- Use one profile for all roles: allowed only if explicitly configured for every role; not implicit.

## Decision: Source/context packets are immutable input receipts

**Rationale**: The PDF emphasizes the digital thread from Discord to GitHub. An immutable packet with Discord message IDs, attachment metadata, transcript status, repository context, and workflow ID gives dedupe, auditability, and restart recovery.

**Alternatives considered**:
- Re-read Discord history on demand: rejected because messages may be edited/deleted and live Discord may be unavailable.
- Embed full attachment bytes in specs: rejected due to size/security; store safe cached paths and metadata.

## Decision: Attachment and voice handling reference existing media/transcription paths

**Rationale**: Hermes already handles Discord attachments, voice messages, caching, and transcription. The factory should capture references and transcript status, not invent media processing.

**Alternatives considered**:
- Require all intakes be text-only: rejected because multi-modal intake is a stated requirement.
- Execute/parse every attachment during gateway intake: rejected because it creates hot-path latency and unsafe side effects.

## Decision: Kanban projection is optional and derived

**Rationale**: Operators may want Kanban notifications/visibility, and Hermes already has a dispatcher/notifier. Projection cards should mirror workflow phases and link back to Spec Kit state/thread, but loss or corruption of Kanban must not corrupt canonical workflow progress.

**Alternatives considered**:
- No Kanban integration: rejected because optional projection is requested.
- Worker-lane/card graph as workflow engine: rejected by corrected scope.

## Decision: GitHub handoff is a contract, not a required live side effect in local steel thread

**Rationale**: The factory ultimately hands off to GitHub, but local development must not touch remote git or credentials. A PR target contract can be validated locally and later executed with explicit approval.

**Alternatives considered**:
- Create GitHub issues/PRs during intake: rejected because specify and handoff are high-impact operations requiring approval and credentials.
