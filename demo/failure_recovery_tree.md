# Failure Recovery Tree

Quick reference for on-stage failures. Each top-level bullet is a symptom. Walk the indented bullets in order. Do not skip levels. The audience would rather see you recover cleanly than see you panic.

Golden rule: if a recovery path would take longer than 30 seconds, abort the live path and switch to the backup video (see `obs_config.md` for the file location).

---

## `/toolforge UI` hangs longer than 10 seconds

- Wait until exactly 12 seconds total
- Say the memorized line: "ToolForge is failing over to the offline cache, you will see five tools in a moment, this is the path you would get on a plane."
- If the fallback appears within the next 10 seconds
    - Continue the demo from the ranked list as if nothing happened
    - Note the run as `partial` in `rehearsal_log.md` afterward
- If the fallback does NOT appear after 25 seconds total
    - Hit Esc to kill the hang
    - Re-run `/toolforge UI`
    - If the fallback fires on the second try, continue
    - If it still fails, say "we will cover this case in v0.2" and continue
    - Paste manually the shadcn-ui-mcp install command from `fallback/ui.json`:
        ```
        claude mcp add shadcn-ui -- npx -y @jpisnice/shadcn-ui-mcp-server
        ```
    - Proceed to Step 5 of the script

---

## Install command refused by argv allow-list

- This LOOKS like a failure but it is the security boundary doing its job
- DO NOT try to bypass the allow-list on stage
- Display the stderr to the audience as proof:
    - "What you are seeing is ToolForge refusing to execute a command that is not on its allow-list. This is the boundary that stops a hostile fallback from running arbitrary shell."
- Recovery to keep the demo moving:
    - `cp PricingCard.reference.jsx PricingCard.jsx`
    - Continue to Step 6 (the rating step) as if the install had succeeded
    - The audience sees the same end state

---

## PostToolUse counter does not increment

- The right-side agent finished editing but the counter at the top of the toolforge status bar still reads 0
- Do not point at the counter and wait, it will not catch up on its own
- Trigger the counter manually:
    - `/toolforge-rate 5`
- The counter increments by 1, the rank for the tool used updates
- Continue with the demo, do not explain the manual trigger to the audience

---

## Right-side agent produces an ugly card

- This is good. Use it.
- Run `/toolforge-rate 1` out loud so the audience sees the score drop
- Then say: "And that is why the ranking learns. The next time someone calls /toolforge UI, this tool ranks lower."
- Re-run `/toolforge UI`
- Point to the now-lower position of the offender in the ranked list
- This turns a real failure into the strongest moment of the talk

---

## Network fails mid-demo

- `/toolforge UI` should fall back automatically (see network_off_rehearsal.md)
- If the network drops between Steps 2 and 3
    - The install in Step 2 already completed, the local plugin is fine
    - Step 3 will hit the fallback path
    - Use the "failing over to the offline cache" line
- If the network drops during Step 2's install itself
    - Wait 5 seconds for the install to retry
    - If retry succeeds, continue
    - If retry fails, switch to the backup video and narrate over it
- Do not turn the phone hotspot on mid-demo, the wifi negotiation will hang the laptop for 10 to 20 seconds. The hotspot is for the green room, not the stage.

---

## SHA-256 integrity check fails

- This is the highest-stakes failure mode. The fallback file's hash did not match `manifest.sha256`.
- ABORT the live install path immediately
- Do NOT proceed with auto-install. The plugin is correctly refusing to load tampered fallback data and the right behavior is to respect that boundary on stage.
- Say: "ToolForge is refusing to load a fallback whose hash does not match its signed manifest. That is the security boundary working as designed. I will install the tool manually for the demo."
- Manually paste the shadcn-ui-mcp install command, exactly:
    ```
    claude mcp add shadcn-ui -- npx -y @jpisnice/shadcn-ui-mcp-server
    ```
- Continue from Step 5
- After the talk, BEFORE the next rehearsal:
    - Verify the manifest by hand: `cd toolforge && sha256sum -c fallback/manifest.sha256` (Windows: `python -c "import hashlib,pathlib; [print(hashlib.sha256(p.read_bytes()).hexdigest(), p.name) for p in sorted(pathlib.Path('fallback').glob('*.json'))]"` and diff against the manifest)
    - Find which file does not match (the `sha256sum -c` output lines that do not end in `OK`)
    - Either restore the file from git (`git checkout -- fallback/<file>.json`), or regenerate the manifest with `cd toolforge && sha256sum fallback/*.json > fallback/manifest.sha256` (Windows: the Python one-liner above, redirected to the manifest file)
    - Log the run in `rehearsal_log.md` as `fail` with `Step that broke = integrity check`

---

## Catastrophic failure (laptop frozen, terminal unresponsive, projector lost signal)

- Do not reboot on stage, you will eat the rest of your slot
- Switch the projector input to the secondary device (your slide deck machine) if available
- Cue the backup video from `toolforge_demo_<latest>.mp4`
- Narrate over the video using the speaker notes
- Land the close line from Step 7 regardless
- Thank the audience, walk off, debug in the green room
