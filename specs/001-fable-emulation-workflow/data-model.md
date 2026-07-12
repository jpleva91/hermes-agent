# Data Model: Fable Emulation Workflow

## Workflow Spec

Represents the durable planning packet for a workflow.

Fields:
- `feature_id`: e.g. `001-fable-emulation-workflow`
- `spec_path`: repository-relative path to `spec.md`
- `plan_path`: repository-relative path to `plan.md`
- `tasks_path`: repository-relative path to `tasks.md`
- `status`: `draft`, `planned`, `groomed`, `in-progress`, `review`, `done`, `blocked`
- `source_artifacts`: Obsidian/Tailnet/NotebookLM URLs and paths

Validation:
- Must include spec, plan, tasks before implementation cards are dispatched.
- Must note unresolved clarifications or blockers.

## Kanban Board

Represents the execution ledger for the workflow.

Fields:
- `board_name`
- `purpose`
- `root_card_id`
- `assignee_roster`
- `dashboard_url` or CLI inspection instructions

Validation:
- Board must be dedicated or explicitly chosen.
- Available assignees must be verified before cards are ready.

## Kanban Card

Represents an independently claimable work unit.

Fields:
- `task_id`
- `title`
- `assignee`
- `status`
- `parents`
- `workspace`
- `body`
- `acceptance_criteria`
- `evidence_required`
- `side_effect_authority`

Validation:
- Owner exists or card is blocked/triaged.
- Workspace is durable if downstream artifacts are needed.
- Side-effect authority is explicit.
- Review cards depend on implementation/research parents.

## Worker Lane

Represents a profile or external poller identity.

Fields:
- `name`
- `kind`: `hermes-profile`, `external-poller`, `human`
- `model`
- `allowed_work`
- `auto_spawn_allowed`
- `required_skills`

Validation:
- Names must match discovered profiles or explicit external poller config.
- Do not assume a profile should auto-spawn if user operating model assigns it to a live external agent.

## Evidence Bundle

Represents the final proof packet.

Fields:
- `artifacts`
- `sources`
- `commands_run`
- `tests_or_checks`
- `diff_or_files_changed`
- `review_verdict`
- `known_caveats`
- `approval_state`

Validation:
- Must be sufficient for Jared or a reviewer to verify without re-running all discovery.
