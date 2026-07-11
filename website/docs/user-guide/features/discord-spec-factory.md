---
title: Discord Spec Factory
---

# Discord Spec Factory (local steel thread)

The Discord Spec Factory is a Hermes-native, disabled-by-default gateway intake path for turning a Discord request into repository-local Spec Kit contract-seed artifacts and a dedicated workflow thread. Tests use synthetic Discord events, temporary repositories, and fake adapters; no live Discord, GitHub, Kanban, gateway restart, remote git, or credential access is required. The repository-local `.specify/workflows/speckit/workflow.yml` contract is loaded and validated, but the official Spec Kit workflow engine/scripts are not executed yet.

## What is canonical

Repository-local Spec Kit seed artifacts under the target repository are the source of truth for this steel thread:

- `.hermes/discord-spec-factory/source-packets/<workflow_id>.json` stores the immutable Discord source/context packet.
- `specs/<workflow_id>-<slug>/workflow-state.json` stores helper workflow phase state, artifact paths, gates, and events.
- A Discord workflow thread, Kanban cards, and PR handoff are projections or operator interfaces, not canonical state.

## Activation guardrails

Production intake must stay disabled until an operator supplies concrete bindings:

- `enabled: true` is required.
- `channel_id` must be a concrete Discord channel/forum ID. Empty values, wildcards, and `DISCORD_SPEC_FACTORY_INTAKE_CHANNEL_ID` fail closed.
- At least one `allowed_role_ids` or `allowed_user_ids` entry is required.
- `repository.path` and `repository.default_branch` are required before workflow initialization and handoff.
- Existing Discord `channel_prompts`, `channel_skill_bindings`, and adapter `create_handoff_thread` are the intended gateway surfaces; this feature does not introduce a second gateway state engine.

The binding lives under `discord.spec_factory` in `~/.hermes/config.yaml`. Keep it disabled until every placeholder is replaced:

```yaml
discord:
  spec_factory:
    enabled: false
    channel_id: DISCORD_SPEC_FACTORY_INTAKE_CHANNEL_ID
    allowed_role_ids:
      - DISCORD_MAINTAINER_ROLE_ID
    allowed_user_ids: []
    repository:
      path: /absolute/path/to/target-repository
      default_branch: main
    kanban_projection:
      enabled: false
      mode: disabled
```

Setting `enabled: false` disables both new intake and registered workflow-thread gate replies. The gateway rejects placeholders, empty/wildcard channels, missing allowlists, unmarked repository roots, and path/symlink escapes.

## Local validation

Run the focused local steel-thread tests from the repository root:

```bash
PYTHONPATH=. python -m pytest \
  tests/hermes_cli/test_discord_spec_factory.py \
  tests/gateway/test_discord_spec_factory_threads.py \
  -q -o 'addopts='
```

The tests verify:

- placeholder intake bindings fail closed;
- synthetic Discord events produce deterministic source packets;
- repository-local Spec Kit seed state is initialized and resumable without claiming official workflow execution;
- exact duplicate source packets resume existing state without overwrite;
- edited same-message source content is recorded as an observation without mutating the original packet;
- repository writes are confined to a real marked repository and reject symlink escapes;
- gateway intake calls the existing `create_handoff_thread` seam exactly once per deduplicated source message and persists the returned thread ID;
- approval gate decisions require an authorized actor and registered workflow thread;
- role/profile/model dispatch packets validate profile existence in dry-run mode;
- Kanban projection can be disabled as a derived no-op;
- PR handoff packets include source, Spec Kit, Discord approval, test evidence, and rollback sections while remote effects remain disabled.

## Operator migration and rollback checklist

Before enabling any live Discord intake:

1. Create the required Hermes profiles outside this workflow and verify their names with a dry-run route validation.
2. Replace the placeholder channel ID with a concrete Discord channel/forum ID.
3. Add explicit maintainer role/user allowlists.
4. Reuse the configured Discord channel prompt and skill binding mechanisms for Spec Kit prompts/skills.
5. Run the focused tests above against a temporary `HERMES_HOME` and temporary target repository.
6. Confirm Kanban projection mode is `disabled` or `notify-only` unless operators explicitly want mirror cards.
7. Confirm GitHub handoff and local-git routes remain blocked until a persisted approval gate is recorded.
8. Treat official Spec Kit workflow execution as unwired until `.specify` scripts/workflows are connected through a reviewed safe integration.

Rollback is local-first:

1. Disable the intake binding or restore the placeholder channel ID.
2. Leave existing Spec Kit feature directories intact for auditability.
3. Stop consuming thread replies by removing the thread registry entry under `.hermes/discord-spec-factory/thread-registry.json` if needed.
4. Delete only derived projection artifacts (Kanban cards, handoff packets) when safe; do not delete source packets unless the repository owner approves an audit-data purge.
5. Re-run the focused tests to confirm disabled Kanban and fail-closed bindings still pass.
