# Quickstart: Fable Emulation Workflow

## 1. Confirm Spec Kit artifacts

```bash
cd /home/red/.hermes/hermes-agent
ls specs/001-fable-emulation-workflow
```

Expected files:

```text
spec.md
plan.md
research.md
data-model.md
quickstart.md
contracts/kanban-card-contract.md
checklists/requirements.md
tasks.md
```

## 2. Confirm available Kanban profiles

```bash
hermes profile list
```

Use only actual profiles, such as `posresearch`, `possynthesis`, `poscoding`, `posreview`, `posops`, `default`, and `readybench` when appropriate.

## 3. Create or select board

Recommended first dogfood board:

```bash
hermes kanban init fable-emulation-workflow
```

If the board already exists, list it instead:

```bash
hermes kanban --board fable-emulation-workflow list
```

## 4. Groom cards from `tasks.md`

Create a root/spec card, then create research/discovery cards in parallel, implementation cards with true parents, and review/synthesis cards gated on their parents.

## 5. Dispatch only after preflight

Before making a card ready, answer:

- Who owns it?
- Where will durable output live?
- How will the next actor verify it?
- Is auto-spawn allowed for that assignee?

## 6. Final handoff

Report:

- spec path
- board name
- card IDs
- running/blocked/done state
- evidence artifacts
- approval needed from Jared
