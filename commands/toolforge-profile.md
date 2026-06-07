# /toolforge-profile

View and manage your adaptive user preference profile. ToolForge learns which skills you prefer per task type over time and uses that to re-rank routing suggestions.

## Usage

```
/toolforge-profile
/toolforge-profile shortcuts
/toolforge-profile detect
/toolforge-profile feedback <task_type_id> <skill_name> good|bad
/toolforge-profile top <task_type_id>
```

## Sub-commands

| Command | What it does |
|---------|--------------|
| _(no args)_ | Show full preference profile across all task types |
| `shortcuts` | List all detected workflow shortcuts |
| `detect` | Scan pipeline history and auto-save new shortcuts |
| `feedback <task> <skill> good\|bad` | Record explicit feedback for a skill |
| `top <task_type_id>` | Show top 5 preferred skills for a task type |

## How it works

ToolForge tracks which skills you use in each session and what task type those sessions belong to. Positive signals accumulate over time; unused or rejected skills decay. The `session-end-learner` hook fires automatically at session end to feed new signals.

Run `/toolforge-profile` after a few sessions to see your profile take shape.

---

```bash
# Show full profile
python bin/toolforge_user_profile.py profile

# List shortcuts
python bin/toolforge_user_profile.py shortcuts

# Scan for new shortcuts (requires 4+ sessions with same skill sequence)
python bin/toolforge_user_profile.py detect_shortcuts

# Record explicit signal
python bin/toolforge_user_profile.py record_signal frontend-ui shadcn-ui-mcp 1

# Top preferences for a task type
python bin/toolforge_user_profile.py top_prefs data-analysis 5
```
