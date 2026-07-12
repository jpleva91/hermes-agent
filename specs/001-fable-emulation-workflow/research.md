# Research: Fable Emulation Workflow

## Decision: Treat Fable 5 as a behavior target, not a dependency

**Rationale**: Jared clarified that Fable 5 has been banned/unavailable. The system should emulate observable workflow qualities rather than depend on direct model access.

**Alternatives considered**:
- Route work to Fable 5 when possible — rejected because availability is the point of the emulation problem.
- Build a new plugin immediately — rejected until Kanban-native dogfood proves which enforcement is needed.

## Decision: Use Hermes Kanban as execution backbone

**Rationale**: Existing Hermes Kanban already provides durable cards, dependencies, comments, runs/logs, dispatcher integration, worker lanes, stale/crash reclaim, and gateway visibility. This matches the dynamic fan-out/fan-in behavior better than transient delegation alone.

**Alternatives considered**:
- Use only `delegate_task` — rejected because it is not durable if the parent turn is interrupted.
- Use only Obsidian/NotebookLM — rejected because those are knowledge/synthesis surfaces, not execution queues.

## Decision: Use Spec Kit before board grooming

**Rationale**: This is a policy-heavy cross-agent workflow with safety, routing, state, and approval boundaries. Spec Kit gives a stable artifact set before workers start.

**Alternatives considered**:
- Create cards directly from chat — rejected because ambiguity and owner/workspace preflight would be underspecified.

## Decision: Route models by task economics

**Rationale**: GPT-5.5/Codex is the available strong lane; cheaper/fast profiles can handle routine triage; expensive/high-effort lanes should be reserved for high-value planning/review. Fable is not assumed available.

**Alternatives considered**:
- One model for everything — rejected due to cost, quota, and context risk.

## Decision: Require fresh-context review for code-changing cards

**Rationale**: Self-review is unreliable. Fresh reviewers and deterministic checks are essential to emulate trustworthy long-running workflow behavior.

**Alternatives considered**:
- Worker self-certification — rejected for merge/deploy-worthy work.
