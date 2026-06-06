# ToolForge v0.2 — Working Docs

Living doc set for the v0.2 router-first push. Written 2026-05-25 after Day-1 validation confirmed 79% skill-dead-rate thesis (177 transcripts, 14,007 tool_use blocks, 28 Skill fires).

## Doc map

| # | Doc | When to read it |
|---|-----|-----------------|
| 01 | [END_PRODUCT.md](./01_END_PRODUCT.md) | Want to know what "done" looks like at v0.2 / v0.3 / v1. Success metrics + scope guardrails. |
| 02 | [EXECUTION_PLAN.md](./02_EXECUTION_PLAN.md) | About to pick up next work item. Sequencing, dependencies, what's done, what's left, time estimates. |
| 03 | [MINDSET_AND_SKILLS.md](./03_MINDSET_AND_SKILLS.md) | About to start coding / planning / reviewing. Which Claude skill to invoke at each phase, decision framework, anti-patterns to dodge. |
| 04 | [RISKS_AND_MITIGATIONS.md](./04_RISKS_AND_MITIGATIONS.md) | When something feels risky, when something IS broken, or before merging a high-blast-radius change. Failure modes + detection + repair playbooks. |

## How these docs were produced

- v0.2 plan v2 (router-first reframe) at `~/.gstack/projects/Tool-Forge/soham-main-plan-v2-20260525-195644.md` is the upstream source of truth for scope.
- Original audit-first design at `~/.gstack/projects/Tool-Forge/soham-main-design-20260525-193744.md` shows the call that was reversed by the LLM Council peer-review round.
- Day-1 empirical validation (commit `99f5cb4`) is the load-bearing evidence under every "ship the router" claim downstream.

## Update cadence

These are durable artifacts. Update them when:
- A risk in `04_RISKS_AND_MITIGATIONS.md` materializes → log the outcome + repair, add a new entry if a new failure mode showed up.
- A new feature lands → tick the checkbox in `02_EXECUTION_PLAN.md`, note the commit hash.
- A scope decision changes the vision → revise `01_END_PRODUCT.md` and link the decision in the commit message.
- A new Claude skill enters the toolbox or one is deprecated → patch `03_MINDSET_AND_SKILLS.md`.

These docs are NOT a CHANGELOG. CHANGELOG is in `../../CHANGELOG.md`. These are forward-looking; CHANGELOG is backward-looking.

## Quick-start for the next session

1. Read `02_EXECUTION_PLAN.md` → find the top `pending` item.
2. Open `03_MINDSET_AND_SKILLS.md`, jump to the phase that matches the item (e.g. "Hook authoring", "DB schema work", "UI polish").
3. Invoke the listed skill BEFORE writing code. This is Gate 1 from the global CLAUDE.md.
4. While building, scan `04_RISKS_AND_MITIGATIONS.md` for the relevant category — preempt the failure mode in code, not in postmortem.
5. Before declaring done: smoke-test against the success metric in `01_END_PRODUCT.md` for the version you're shipping.

## Single-question lookup

| Question | Doc |
|----------|-----|
| What's the next thing to build? | 02 |
| Is this scope creep? | 01 (kill list section) |
| Which skill should I use right now? | 03 |
| This thing broke — known issue? | 04 |
| How do I know I'm done? | 01 (success metrics) |
| Can I bundle these three features? | 02 (commit-grouping rules) |
| Is autonomy gain worth the trust cost? | 04 (autonomy-risk table) |
| What did the LLM Council kill and why? | 01 (anti-vision) |
| Why is my router latency >100ms? | 04 (R1.2 repair playbook) |
| Why does my router suggest the same skill every time? | 04 (R1.3 self-poisoning) |
| How do I know the 79% dead-rate is real? | 04 (R1.1) → run hand-label gate from 02 (Phase A.1a) |
| What's "shadow mode" and why does v0.2 ship in it? | 01 (router_mode flag), 02 (Phase B step 5), 04 (R2.2 demo metrics) |
