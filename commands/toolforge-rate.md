---
description: Rate the most recently installed ToolForge tool on a 1 to 5 Likert scale.
argument-hint: <1-5>
---

You are running the `/toolforge-rate` command. The user passed: **$ARGUMENTS**.

Validate $ARGUMENTS matches the regex `^[1-5]$` (a single digit 1 through 5). If it does not match, tell the user "Valid ratings: 1, 2, 3, 4, 5" and stop. Do NOT pass through arbitrary strings.

Then use the Bash tool with the rating passed as a separate, fully-quoted positional argument (NOT string-interpolated into a larger composed shell command):

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_rate.py" "$ARGUMENTS"
```

The wrapper re-validates the regex at the Python layer and refuses anything not in `^[1-5]$`. Print the wrapper's stdout to the user.

Bash-tool argument quoting is **defense in depth, not a security boundary**. The Python-side regex is the actual defense. Quote anyway because layered defense costs nothing.
