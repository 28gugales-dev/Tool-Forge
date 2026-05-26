# End-Product Vision

What "done" looks like at each version. Use this as the **scope guardrail**: any feature whose impact doesn't reduce one of the success metrics below is scope creep — defer or refuse.

---

## v0.2 — Minimum viable router

**Headline**: "ToolForge sees what you're about to do and recommends the right skill BEFORE you finish the prompt."

### Pitch in one sentence

`/toolforge ui` is the v0.1 way. In v0.2 you don't type the slash command — you describe the task, and a hook silently injects the ranked top-3 candidates as Claude's working context. The 79% dead-skill rate drops because Claude now sees the matching skill BEFORE it falls back to ad-hoc Edit + Bash.

### Mandatory features (ship gate)

1. **`hooks/user-prompt-router.py`** — UserPromptSubmit hook that scores user message against every installed **skill** (skills only in v0.2 — MCPs/plugins/agents/commands routed in v0.3) description via tf-idf cosine. Top-3 candidates above threshold get injected as `<system-reminder>` with the line: `"Skills that may match: <a>, <b>, <c>. Invoke via Skill tool if relevant."`
2. **`bin/toolforge_router.py`** — the scoring engine the hook calls. Pure stdlib tf-idf. Reads installed inventory from existing `webui/inventory.py:build_inventory()` (now disk-cached). Caches the corpus index at `tempdir/toolforge_router_idx.json`, 1hr TTL.
3. **`bin/toolforge_verify_install.py`** — post-install assertion. Runs `claude mcp list` / `claude plugin list`, greps for the just-installed name. If absent → exit 4, prints `not registered, manual debug needed`. Single source of "did it actually work?".
4. **`bin/toolforge_rate_inferrer.py`** — passive Likert from `usage_stats`. Tools fired ≥5 times in 30d w/ no negative explicit rating → infer rating 4, `source='inferred'`, weight 0.5×. Solves cold-start.
5. **`router_mode: shadow|active` config flag** — ship v0.2.0 in shadow mode (log-only, inject nothing). Promote to active after 7 days of shadow data shows <15% false-positive rate. **Demo metrics traceable to shadow log**, not asserted.
6. **README rewrite** — opens with "Stop typing slash commands. ToolForge auto-routes." Pre-v0.2 framing (audit-first) demoted to bottom. Every percentage cited must trace to a measured number in the shadow log or hand-label exercise.

### Success metrics (measurable, gated)

| # | Metric | Target | How to measure |
|---|--------|--------|----------------|
| M0 | **`[CRITIQUE]` Hand-label gate** | ≥12/20 prompts show a missed-skill trigger | 3hr manual exercise; output saved to `~/.gstack/projects/Tool-Forge/handlabel-results-*.md`. **If <8/20 → v0.2 thesis fails, pivot.** |
| M1 | Skill-fire rate in routed sessions (post-active) | ≥5% of `tool_use` blocks (vs 0.2% pre-router) | Re-run `bin/toolforge_dumb_scanner.py` on 7d of post-active-mode transcripts; compare to baseline |
| M2 | Router latency p95 | <80ms per user prompt | Inline timer in hook, dumped to `~/.claude/toolforge_router.log` |
| M3 | False-positive rate (in shadow mode, gating active mode) | <15% over 7d of shadow data | Manual label of 50 shadow-log decisions before flipping `router_mode: active` |
| M4 | Self-install round-trip | 1 user click → tool registered, smoke-tested, ready on restart | Manual: 5 fresh installs of MCPs across categories; verify_install confirms |
| M5 | Rate-inferrer coverage | 60%+ of installed tools have `n>0` after 30 days passive | DB query: `SELECT COUNT(*) FROM ratings WHERE source='inferred' OR source='user'` vs `SELECT COUNT(*) FROM usage_stats` |
| M6 | **`[CRITIQUE]` Top-1 suggestion share** | <50% over rolling 7d (no single skill dominates) | DB query on `usage_stats` filtered to router-attributed rows. >50% for 3d → R1.3 self-poisoning fired, run repair playbook. |

### Stretch (ship if time, not gate)

- Stack-aware mid-task suggestions (PostToolUse hook reads filename extensions → suggests stack-matching skills)
- Auto-deprecate flag (zero invocations in 90d + last_commit >180d → list in `/toolforge-status` for one-click uninstall)
- Drift detection ("this skill's fire-rate dropped 5x in 14d — want to rerate?")

### Anti-vision (will NOT build in v0.2)

These were considered and **explicitly rejected** in the LLM Council pass:

| Rejected | Why |
|----------|-----|
| Cloud sync / accounts / shared ratings registry | Privacy-local is the moat. Council unanimous. |
| Telemetry on installs / pings home | Same. NEVER for this product. |
| Auto-install without ANY consent (even for low-risk types) | Trust boundary. ONE-click batch is the floor; zero-click crosses into supply-chain attack surface. |
| `/api/audit` web endpoint | Demoted to side panel in plan v2 — audit is byproduct of router, not headline. |
| Marketplace / community ratings ingestion | Cloud drift. v0.3+ optional. |
| Gap detector ("you're missing X for stack Y") | Over-promised in plan v1. Audit-first thinking. Re-derive from router data instead. |
| README rewrite into full ARCHITECTURE replacement | Plan v1 mistake. Keep ARCHITECTURE.md, rewrite README hero only. |
| Pre-rec'd forge demo as v0.2 gate | Move to v0.3. v0.2 ship is router + verify + rate-infer; demo is distribution work, separate concern. |
| **`[CRITIQUE]`** Routing MCPs / agents / commands in v0.2 | Skill routing only. MCPs are passive pools; commands have `/slash` invocation already; agents are spawned explicitly. Routing them produces noise that obscures the validation metric. Add in v0.3 once skill routing is empirically tuned. |
| **`[CRITIQUE]`** Webui sliders for router threshold / weight tuning | Two days of UI work for a feature 0% of v0.2 users need. Config file edits are correct UX for power users. Stays cut. |
| **`[CRITIQUE]`** Demo metrics asserted from intuition | "Router fires 78% vs 25% baseline" with no methodology = top HN comment killer. Every percentage cited in demo or README must trace to a logged measurement (shadow log or hand-label). |

---

## v0.3 — Distribution + polish

**Headline**: "It's good and people who don't know you actually install it."

### Targets

1. **Pre-recorded forge demo** (60-90s GIF/video for README hero) showing the auto-route in action.
2. **Stack-aware mid-task suggestions** promoted from v0.2 stretch to v0.3 mandatory.
3. **Auto-deprecate flag UX**: side panel in `/toolforge-status` showing "5 tools dormant — uninstall?" → one-click batch.
4. **Hot-reload sanity** — document the exact Anthropic-CLI restart/reload semantics so users know when restart is needed. Or eliminate restart via in-session `claude mcp reload <name>` (if available; otherwise document).
5. **Marketplace listing**: push to claudemarketplaces.com with the new framing.
6. **Author 3 reference flows in webui** that ship pre-built (e.g. "UI polish pipeline: impeccable → design-review → 21st.dev").

### Success metric

- **30 GitHub stars in first 30 days** (vanity but trackable)
- **5 unsolicited install reports** in the demo's Discord/X thread (true validation)
- **Council re-run on v0.3 plan before build**: re-test the path with fresh advisors

---

## v1.0 — Network-effects unlock (12-month horizon)

**Headline**: "ToolForge is how Claude Code users find new tools, full stop."

### Required to be called v1

- Router quality competitive with hand-typed slash commands (false-positive rate <5%, latency <50ms)
- ≥500 installed users (tracked via opt-in anonymous heartbeat — DIFFERENT from rejected v0.2 telemetry; consent-first, opt-in, no PII)
- Drift detection actually catches a real stealth-breakage in the wild (proven via case study)
- One marquee MCP author has integrated their server into the ToolForge fallback dataset

### Explicit non-goals at v1

- Still no cloud sync.
- Still no accounts.
- Still no SaaS pivot. If revenue happens, it's via sponsored fallback slots (clearly marked, opt-in) or a paid premium "private corporate dataset" tier — NOT user accounts on a cloud DB.

---

## North star (every version)

**For Claude users with installed tools, ToolForge ensures the right one fires when it should.** Not curate-more-tools-to-install (that's discovery, v0.1 problem). Not maintain-a-clean-toolbox (that's deprecation, side benefit). Make the installed tools EARN their disk space by actually firing when they match the work.

If a feature doesn't move M1 (skill-fire rate), it doesn't belong in v0.2. Period.
