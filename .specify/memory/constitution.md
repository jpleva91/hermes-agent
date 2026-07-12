# Hermes Agent Constitution

## Core Principles

### I. Durable Agent State Before Autonomous Action
All long-running or multi-agent work MUST have durable state before execution. Kanban cards, task comments, logs, run metadata, Obsidian notes, or repository specs are the source of truth; transient chat context and scratch workspaces are not sufficient for work that must survive restarts, dispatch retries, or human review.

### II. Evidence-Gated Execution
Agents MUST return verifiable evidence for meaningful claims: tests, diffs, logs, screenshots, source citations, task IDs, run IDs, or artifact paths. Important changes cannot be considered complete from prose alone.

### III. Human Approval for External Side Effects
Merges, deployments, production changes, account actions, public posting, private data upload, and external side effects require explicit human approval unless a task grants that authority in unmistakable terms. Verification cards authorize observation and reporting, not production changes.

### IV. Profile and Workspace Isolation
Multi-agent execution MUST use actual configured profiles and durable workspaces. Unknown assignees, scratch artifacts cited as durable output, and branch-mutating operations in shared checkouts are invalid. Coding work uses repo directories or isolated worktrees; downstream cards must know where durable output lives.

### V. Skills Capture Proven Workflows
Procedural knowledge belongs in skills after a workflow proves useful. Memory stores stable facts only; skills store reusable procedures. Any repeated or non-trivial workflow discovered during execution should be promoted into a skill or existing skill patch.

## Operational Constraints

- Prefer existing Hermes surfaces before adding plugins: CLI, gateway, Kanban, cron, skills, hooks, profiles, comments, runs, logs, and Tailnet/Obsidian reporting.
- Treat model output as untrusted until verified by deterministic checks or fresh-context review.
- Keep model routing cost-aware: reserve expensive/high-effort lanes for high-leverage planning, implementation, or review; use cheaper lanes for routine triage and summaries.
- Preserve profile isolation. Do not edit another profile's skills/plugins/cron/memories without explicit direction.
- Respect repository development guidance in AGENTS.md and project-specific docs.

## Development Workflow

1. Start policy-heavy, cross-cutting, or multi-agent workflow changes with Spec Kit artifacts.
2. Convert accepted specs into Kanban cards with exact owners, durable workspaces, dependencies, acceptance criteria, and evidence requirements.
3. Dispatch only after preflight answers are known: owner, workspace, verification method, and whether auto-spawn is allowed.
4. Require review gates for code-changing cards before merge/deploy.
5. Update Obsidian/Tailnet/skills after meaningful workflow changes.

## Governance

This constitution governs Spec Kit planning for Hermes Agent workflow changes. Amendments require an explicit note in the relevant spec or plan explaining what changed and why. Plans and Kanban grooming must document any constitution violation and the simpler alternative rejected.

**Version**: 1.0.0 | **Ratified**: 2026-06-13 | **Last Amended**: 2026-06-13
