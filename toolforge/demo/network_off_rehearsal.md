# Network-Off Rehearsal

The goal of this dry run is to prove that `/toolforge UI` degrades gracefully when the laptop has zero connectivity. Run it at least twice and log both runs in `rehearsal_log.md` before going on stage.

## 1. Take the network all the way down

Do all three. Wifi alone is not enough, the OS may still route over a cached profile.

- Wifi off (system menu, not just disconnect)
- Ethernet cable unplugged if any
- Airplane mode ON

## 2. Verify the network is actually off

Run these two checks. Both must fail.

```
ping 1.1.1.1
```
Expected: 100 percent packet loss, or "Network is unreachable".

```
nslookup anthropic.com
```
Expected: timeout or "server can't find" error.

If either succeeds, you still have a route somewhere. Find it and kill it before continuing.

## 3. Run the demo step that depends on discovery

```
/toolforge UI
```

### Expected behavior

- A short attempt to reach the live discovery endpoint
- A clean timeout, no stack trace, no red text
- Immediate fallback to `fallback/ui.json`
- Exactly 5 entries shown in the ranked list
- A banner reading `Live discovery unavailable, showing cached results from <timestamp>`

Anything other than this is a defect. Stop the rehearsal and fix the plugin before you run again.

## 4. Confirm the fallback integrity check ran

The integrity check is invoked by `toolforge-curator` SKILL.md (step 7), which shells out to `sha256sum -c "${CLAUDE_PLUGIN_ROOT}/fallback/manifest.sha256"` before loading any fallback JSON. To verify by hand outside the skill:

```
cd toolforge
sha256sum -c fallback/manifest.sha256
```

Expected output: 5 lines, all ending in `OK`. Anything ending in `FAILED` is a hash mismatch, and the curator will refuse to load that file at runtime.

Windows note: if `sha256sum` is not on PATH, compute hashes per file:

```
python -c "import hashlib, pathlib; [print(hashlib.sha256(p.read_bytes()).hexdigest(), p.name) for p in sorted(pathlib.Path('fallback').glob('*.json'))]"
```

Compare each output line against the corresponding line in `fallback/manifest.sha256` by eye.

## 5. Recovery plan if integrity check fails on stage

The integrity check failing means the shipped JSON does not match `manifest.sha256`. Two causes.

- You edited a JSON without regenerating the manifest. Always regenerate after editing any fallback file: `cd toolforge && sha256sum fallback/*.json > fallback/manifest.sha256` (Windows: redirect the Python one-liner above into `fallback/manifest.sha256`).
- The file is genuinely tampered. In that case the plugin is doing the right thing by refusing to load it.

On stage, do NOT try to bypass the check. Instead:

1. Acknowledge: "ToolForge is refusing to load a fallback file whose hash does not match its manifest. That is the security boundary working."
2. Manually paste the shadcn-mcp install command from your memorized backup.
3. Continue the demo from Step 5 of the script.

Before every stage run, you must have a precomputed `manifest.sha256` matching the shipped JSONs. Never edit the JSONs without regenerating the manifest. Verify with:

```
cd toolforge && sha256sum -c fallback/manifest.sha256
```

Expected output: 5 lines, every line ending in `OK`. Any line ending in `FAILED` blocks the fallback path at runtime.

## 6. What to say if /toolforge UI hangs longer than 12 seconds

Word for word:

> "ToolForge is failing over to the offline cache, you'll see five tools in a moment, this is the path you'd get on a plane."

Then wait. Do not retype, do not Ctrl-C unless the fallback has not appeared after 25 seconds total. If you Ctrl-C, jump to `failure_recovery_tree.md`.

## 7. Restore connectivity after the rehearsal

- Airplane mode OFF
- Wifi ON, reconnect to your dev SSID
- Re-run `ping 1.1.1.1`, expect replies
- Log the run in `rehearsal_log.md` with `Live or fallback` set to `fallback`
