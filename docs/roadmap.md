# Hermes Agent — Chitin Swarm Roadmap

> **Important**: this is the **chitinhq swarm's fork** of upstream
> NousResearch/hermes-agent. This roadmap describes how WE use the
> fork (cron + watchdog + bridge runtime for the swarm); upstream
> Nous's roadmap is separate.
>
> Per chitin spec 024 §1.3: every active repo carries
> `docs/roadmap.md`.

## Mission (our use)

Hermes Agent is the cron runtime that hosts the swarm's scheduled
jobs (board-watchdog, hermes-clawta-bridge, readybench-poller,
chain-summary, swarm-standup, swarm-retro, agent-bus-inbound-poll
from chitin spec 023, etc.). Cron config lives at
`~/.hermes/cron/jobs.json`; scripts at `~/.hermes/scripts/`.

## Status as of 2026-05-17

**Hot for**: agent-bus inbound poll just landed (spec 023). Several
cron jobs depend on hermes-agent being responsive.

### Active integrations

- `agent-bus-inbound-poll` (1 min) — discord → bus DB ingest (spec 023)
- `board-watchdog` (10 min) — kanban spec-binding + loop detection
- `hermes-clawta-bridge` (15 min) — failure escalation
- `readybench-poller` (15 min) — readybench dispatch
- `autonomous-board-engine` (30 min) — P0/P1 claim for hermes
- `chain-summary` (8a/8p) — governance summary
- `swarm-standup` (9a weekday)
- `swarm-retro` (10a Monday)
- `board-audit` (every 2h) — drift detection

### Recent cron issues hit this session

- **F1**: `discord_push._load_env_once` cached empty webhook map (spec 021 → spec 023 fix)
- **F2**: `discord_mirror.py poll` had no cron entry (spec 023 added `agent-bus-inbound-poll`)
- **Watchdog spec_root drift**: chitin PR #743 + spec 022 (pending)

## Next 4-week milestones

### This week
- [ ] Cron taxonomy doc: name each cron job's contract + invariant
  + which spec binds it (most-active jobs lack a spec binding today)
- [ ] Audit cron jobs against spec 022 dispatch-readiness contract
- [ ] Verify `agent-bus-inbound-poll` cron runs cleanly for 24h

### Week of 2026-05-26
- [ ] Spec the agent-runtime contract — what does hermes-agent
  promise to the swarm? Currently de-facto only.
- [ ] Sentinel-integration: every cron job emits structured exec
  events that sentinel can read (when sentinel revives)

## Dependencies + blockers

- **chitin spec 020/022/023/024** — governance contracts hermes-agent
  must honor (already followed in practice; needs codification)
- **Upstream NousResearch/hermes-agent** — operator periodically
  rebases the chitin fork against upstream; coordination needed
  when upstream changes the cron format or agent runtime

## Out of scope

- Replacing the upstream agent runtime (we use Nous's; only fork
  for our cron config + scripts)
- Multi-host scheduling (single-host operator box; octi is the
  eventual home for cross-host)
- Renaming the runtime (Ares is the persona; hermes-agent is the
  underlying process)

## How to read this if you're a worker

1. Cron jobs at `~/.hermes/cron/jobs.json` — read-only from worker POV
2. Scripts at `~/.hermes/scripts/` — workers update via PR to this
   repo (or to chitin/swarm/bin/ + sync via install-*.sh)
3. Per chitin spec 020 §1.2, any new cron-bound script needs a spec
   + e2e test (e.g. spec 023's `test_bidirectional_liveness_e2e`)
