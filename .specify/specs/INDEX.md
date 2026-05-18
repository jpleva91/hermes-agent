# hermes-agent (chitin swarm fork) — spec-kit INDEX

> Per chitin spec 024 §1.3: every active repo carries `.specify/specs/INDEX.md`.

## Status

Hermes-agent does NOT currently own any specs in its own
`.specify/specs/`. The contracts hermes-agent honors live in
`chitinhq/chitin/.specify/specs/`:

| Cross-repo spec | Where | What it gives hermes-agent |
|------------------|-------|----------------------------|
| chitin 020 | SDD+TDD enforcement | Every new cron script needs a spec + test |
| chitin 022 | Dispatch readiness contract | Cron jobs that touch kanban respect the readiness gates |
| chitin 023 | Agent-bus bidirectional liveness | The `agent-bus-inbound-poll` cron is installed via `swarm/bin/install-agent-bus-cron.sh` |
| chitin 024 | Active-repo doc-bundle | This INDEX file exists because of 024 |

## Pending specs (to be filed)

| Slug (proposed) | Purpose | Notes |
|-----------------|---------|-------|
| `001-cron-taxonomy` | Name each cron job's contract + invariant | Drafted under hermes-agent docs/roadmap.md "next milestones" |
| `002-agent-runtime-contract` | What does hermes-agent promise the swarm? | Currently de-facto only |

## Cross-references

- chitin spec INDEX: [`chitinhq/chitin/.specify/specs/INDEX.md`](https://github.com/chitinhq/chitin/blob/main/.specify/specs/INDEX.md)
- workspace spec INDEX: [`chitinhq/workspace/.specify/specs/INDEX.md`](https://github.com/chitinhq/workspace/blob/spec-kit/overnight-2026-05-17-retro/.specify/specs/INDEX.md)

## Maintenance

- Maintainer: Ares lane in the overnight roadmap
- This INDEX seeds as the cross-repo contracts list; as hermes-agent
  grows its own specs, add an "Active specs" section above
