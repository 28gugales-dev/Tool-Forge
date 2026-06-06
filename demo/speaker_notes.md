# Speaker Notes

Pinned to step numbers in `demo_script.md` (7 steps, total 4 min 30 s). Read each `Say verbatim` line out loud. Point with the cursor, not your hand. Do not improvise on the `DO NOT say` lines.

The 4:30 total is non-negotiable. If you fall behind, drop the optional sentences first, then drop Step 6 entirely (see the bottom).

---

## Step 1 (00:00 to 00:20): Frame the problem

**Say verbatim:** "Anthropic Tool Search picks from the tools you already installed. It does not help you find new ones. We do, live from the web, and we learn which ones actually help you."

**Point to:** The right terminal, idle, ToolForge installed but no UI MCPs.

**DO NOT say:** "Anthropic's marketplace is broken." "MCP is bad." Anything that punches at a partner.

**Time budget:** 20 s. If overrun, you are too slow already, tighten Step 2.

---

## Step 2 (00:20 to 01:10): Same prompt on the left (vanilla)

**Say verbatim:** "Vanilla Claude Code. Same model, same prompt. Watch the result."

**Point to:** The LEFT terminal as Claude streams a generic pricing card into `src/PricingCard.jsx`.

**DO NOT say:** "This is bad code." "Claude is dumb." The vanilla result is acceptable. The point is the gap, not the failure.

**Time budget:** 50 s. Most of this is waiting on the agent. Resist narrating the diff line by line.

---

## Step 3 (01:10 to 01:30): Switch focus to the right

**Say verbatim:** "Now watch what ToolForge does first."

**Point to:** The RIGHT terminal as you type `/toolforge UI`.

**DO NOT say:** "It is calling an LLM." Avoid telemetry or privacy claims unless asked.

**Time budget:** 20 s. The discovery call should return within this window. If it hangs past 12 s, jump to `failure_recovery_tree.md`.

---

## Step 4 (01:30 to 02:00): Approve installs

**Say verbatim:** "Two real MCP servers, installed live, through a sandboxed argv allow-list."

**Point to:** The ranked list (shadcn-ui-mcp at the top), then the install command as it scrolls past, then the success line.

**DO NOT say:** "This is bulletproof." "We have not been audited." Stay accurate. The allow-list is real, not a guarantee.

**Time budget:** 30 s. Pick option 1 then option 2 fast. Confirm with `y` each time.

---

## Step 5 (02:00 to 03:30): Same prompt on the right (with new MCPs)

**Say verbatim:** "Same prompt. Same model. Different tool surface. That is the whole pitch."

**Point to:** The RIGHT terminal as Claude pulls real shadcn primitives and writes a polished card.

**DO NOT say:** "This always works." "The agent is perfect." If the card lands ugly, do not hedge: that is the next step.

**Time budget:** 90 s. If the agent stalls past 70 s, abort and `cp PricingCard.reference.jsx PricingCard.jsx` from `failure_recovery_tree.md`.

---

## Step 6 (03:30 to 04:00): Rate the result

**Say verbatim:** "One number. Stored locally. Feeds the next ranking. If I love it I rate it five, if I hate it I rate it one."

**Point to:** The `/toolforge-rate 5` line, then the confirmation echo.

**DO NOT say:** "This trains a model." It does not train a model. It updates local Bayesian weights.

**Time budget:** 30 s.

---

## Step 7 (04:00 to 04:30): Show the learning loop

**Say verbatim:** "Watch the rating change the ranking. That is self-learning visible in 10 seconds."

**Point to:** `/toolforge-status` output, then a second `/toolforge UI` showing shadcn-ui-mcp ranked higher than the first call.

**DO NOT say:** "Please star us." "Sign up." Keep it dignified, the demo IS the pitch.

**Time budget:** 30 s. Land the close line on time.

---

## If you have 30 seconds left

Add: "ToolForge also works offline. The cache I just used ships with the plugin." Then close.

## If you have run out of time

Skip Step 6 entirely. Jump from Step 5 directly to Step 7. Do not rate, do not show the dashboard. Land the Step 7 close line. The audience would rather you finish on time than see the rating loop.
