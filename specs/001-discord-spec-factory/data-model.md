# Data Model: Hermes-Native Discord-to-Spec-Kit Software Factory

## FactoryIntakeBinding

Represents a configured Discord channel or forum parent where factory requests are accepted.

**Fields**:
- `enabled`: boolean; must be true for activation.
- `channel_id`: string; exact Discord channel/forum ID. Placeholder `DISCORD_SPEC_FACTORY_INTAKE_CHANNEL_ID` is documentation-only and must not activate live intake.
- `allowed_user_ids`: optional list of Discord user IDs.
- `allowed_role_ids`: optional list of Discord role IDs.
- `required_skill_bindings`: list of Hermes skills auto-loaded for the channel.
- `channel_prompt`: ephemeral prompt text or reference to existing `channel_prompts` entry.
- `repository`: repository slug/path used for Spec Kit initialization.
- `default_branch`: PR base branch default.
- `kanban_projection`: disabled, notify-only, or mirror-cards.
- `failure_channel_id`: optional fallback notification channel.

**Validation**:
- Disabled if `enabled` is false, `channel_id` is empty, wildcard, or placeholder.
- At least one authorization constraint must exist for live activation.
- Must name a repository/workspace before GitHub handoff can proceed.

## SourceContextPacket

Immutable captured receipt for one accepted intake message.

**Fields**:
- `schema_version`: integer.
- `workflow_id`: deterministic unique workflow ID.
- `source.platform`: `discord`.
- `source.channel_id`, `source.thread_id`, `source.message_id`, `source.parent_channel_id`.
- `source.author_id`, `source.author_display`, `source.author_roles`.
- `source.created_at`, `source.received_at`.
- `content.text` and `content.text_sha256`.
- `attachments[]`: id, filename, media type, byte size, cached path if available, sha256 if available, supported flag, warning.
- `voice[]`: attachment reference, transcript text if available, transcript status, transcript provider metadata if safe.
- `repository`: repo path/slug, default branch, requested target if provided.
- `discord.workflow_thread_id`: set after thread creation/reuse.
- `spec_kit.feature_directory`: set after local Spec Kit seed initialization.

**Validation**:
- Requires platform/channel/message IDs and received timestamp.
- Packet is append-only; corrections create later events, not in-place mutation.
- Secrets are not stored; cached paths must be local and safe.

## SpecKitWorkflowState

Repository-local workflow seed/helper state persisted in the repository. The official Spec Kit engine/workflow is not executed by the current steel thread.

**Fields**:
- `workflow_id`.
- `feature_directory`.
- `current_phase`: specify, clarify, plan, tasks, implement, converge, review, handoff, done, blocked.
- `phase_status` map.
- `source_packet_path`.
- `artifact_paths`: spec, plan, research, data model, contracts, tasks, evidence.
- `gates[]`: approval/clarification gates with status.
- `events[]`: state transitions and evidence pointers.

**State transitions**:
- `created -> specify` after packet accepted and local Spec Kit seed feature dir exists.
- `specify -> clarify` when initial spec exists.
- `clarify -> plan` only after required answers.
- `plan -> tasks` only after review approval.
- `tasks -> implement` only after route validation.
- `implement -> converge -> review -> handoff -> done` only through gates and evidence.
- Any phase may enter `blocked` with a recoverable reason.

## WorkflowThread

Projection linking a Discord thread to a workflow.

**Fields**:
- `workflow_id`.
- `platform`: discord.
- `parent_channel_id`.
- `thread_id`.
- `thread_name`.
- `created_by_message_id`.
- `status`: pending, active, unavailable, archived.
- `last_notified_at`.

**Validation**:
- Exactly one active workflow thread per workflow.
- Thread failure blocks human gates but does not corrupt Spec Kit state.

## RoleRoute

Declarative route from factory role to Hermes execution profile/model/tool boundary.

**Fields**:
- `role`: intake-triage, spec-lead, architect, implementer, reviewer, release-captain, operator-notifier.
- `profile`: existing Hermes profile name.
- `model`: provider/model or model tier.
- `toolsets`: allowed toolsets.
- `workspace`: scratch, existing worktree, or explicit path.
- `max_turns`, `budget`, `approval_required`.
- `allowed_slug_or_path_prefixes` where relevant.
- `side_effects`: none, local-files, local-git, remote-github.

**Validation**:
- Required roles must be present and enabled before the associated phase starts.
- Missing profile/model/toolset route blocks the phase.
- `local-git` and `remote-github` side effects require approval gates even if route config omits or disables `approval_required`.

## ApprovalGate

Persisted human or policy decision point.

**Fields**:
- `gate_id`, `workflow_id`, `phase`.
- `required_actor_ids`, `required_role_ids`, or a future concrete `required_route` policy.
- `prompt` and `options`.
- `status`: pending, approved, rejected, expired.
- `decision`: value, actor, Discord message ID, timestamp.

**Validation**:
- Unauthorized replies never approve; missing actor/role constraints fail closed in this steel thread.
- Expired/rejected gates leave workflow blocked or return to prior phase.

## KanbanProjection

Optional mirror from canonical workflow phases to Kanban cards.

**Fields**:
- `workflow_id`.
- `enabled` and `mode`.
- `board`, `tenant`.
- `card_links[]`: phase/task to card ID.
- `last_sync_status`.

**Validation**:
- Projection status is derived and recoverable.
- Deleting cards does not delete or rewind Spec Kit state.

## GitHubHandoff

Final handoff target and evidence bundle.

**Fields**:
- `workflow_id`.
- `repository`.
- `base_branch`.
- `feature_branch`.
- `pr_title`, `pr_body`.
- `commits` and `changed_files` if available.
- `test_evidence`.
- `approval_gate_id`.
- `rollback_plan`.

**Validation**:
- Cannot proceed without repository/base branch and approval.
- Must include links to Spec Kit feature directory, Discord thread, and source packet.
