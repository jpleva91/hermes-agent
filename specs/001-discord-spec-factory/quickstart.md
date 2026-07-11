# Quickstart: Local Steel Thread Validation

This guide validates the first bounded steel thread without live Discord, GitHub, gateway restart, remote git, credentials, or writes to the live Hermes home.

## Prerequisites

- Worktree: `/home/red/code/hermes-discord-spec-factory`
- Branch: `feature/discord-spec-factory-v1`
- Spec Kit initialized with `integration=hermes`
- Python dependencies available for repository tests

## 1. Inspect the feature pack

```bash
cd /home/red/code/hermes-discord-spec-factory
find specs/001-discord-spec-factory -maxdepth 3 -type f | sort
```

Expected: the feature directory contains `spec.md`, `plan.md`, `research.md`, `data-model.md`, `quickstart.md`, `tasks.md`, `governance.yaml`, `contracts/*`, and `checklists/requirements.md`.

## 2. Validate fail-closed intake configuration locally

Use a synthetic configuration object with the placeholder channel ID:

```python
from hermes_cli.discord_spec_factory import validate_intake_binding

result = validate_intake_binding({
    "enabled": True,
    "channel_id": "DISCORD_SPEC_FACTORY_INTAKE_CHANNEL_ID",
    "allowed_role_ids": ["role-maintainers"],
})
assert not result.ok
assert "placeholder" in result.reason
```

Expected: placeholder/default channel IDs do not activate intake.

## 3. Build a synthetic source/context packet

```python
from hermes_cli.discord_spec_factory import build_source_context_packet

packet = build_source_context_packet({
    "channel_id": "123456789012345678",
    "message_id": "223456789012345678",
    "author_id": "323456789012345678",
    "author_display": "Maintainer",
    "created_at": "2026-07-10T00:00:00Z",
    "text": "Build a spec-driven Discord intake workflow",
    "attachments": [{"id": "att-1", "filename": "requirements.md", "content_type": "text/markdown", "size": 128}],
}, repository={"path": "/tmp/repo", "default_branch": "main"})

assert packet["source"]["platform"] == "discord"
assert packet["workflow_id"].startswith("dsf_")
assert packet["content"]["text_sha256"]
```

Expected: the packet is deterministic, contains source IDs, hashes the text, and does not require network access.

## 4. Validate role routing contract

```python
from hermes_cli.discord_spec_factory import validate_role_routes

routes = {
    "intake-triage": {"profile": "default", "model": "fast", "toolsets": ["file"]},
    "spec-lead": {"profile": "spec-lead", "model": "reasoning", "toolsets": ["file", "terminal"]},
    "architect": {"profile": "architect", "model": "reasoning", "toolsets": ["file"]},
    "implementer": {"profile": "implementer", "model": "code", "toolsets": ["file", "terminal"]},
    "reviewer": {"profile": "reviewer", "model": "reasoning", "toolsets": ["file", "terminal"]},
    "release-captain": {"profile": "release", "model": "balanced", "toolsets": ["file", "terminal"]},
    "operator-notifier": {"profile": "default", "model": "fast", "toolsets": []},
}
assert validate_role_routes(routes).ok
```

Expected: missing roles fail closed; complete routes pass.

## 5. Run focused tests

```bash
cd /home/red/code/hermes-discord-spec-factory
PYTHONPATH=. python -m pytest tests/hermes_cli/test_discord_spec_factory.py -q -o 'addopts='
```

Expected: all local steel-thread tests pass.

## 6. Rollback local steel thread changes

No live config is changed. To remove the local steel thread before commit, delete:

```text
hermes_cli/discord_spec_factory.py
tests/hermes_cli/test_discord_spec_factory.py
specs/001-discord-spec-factory/
.specify/feature.json
```

Do not delete or modify `/home/red/.hermes/hermes-agent`, live profile config, gateway state, or credentials.
