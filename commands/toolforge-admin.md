---
description: ToolForge admin panel. Manage skill ratings, create/delete skill stacks, manage organisation profiles, run self-management routines (purge stale, auto-retire, rebalance scores), and view the full system health report.
---

You are running the `/toolforge-admin` command.

Parse the sub-command from the user's input. If no sub-command is given, show the help menu.

## Help menu (no sub-command)

```
ToolForge Admin — available sub-commands:

  health                   Full system health dashboard (ratings + perf + predictions + token stats)
  retire <tool>            Force-retire a tool (insert 5×1-star ratings)
  override <tool> <score>  Override a tool's effective rating (1.0–5.0)
  reset <tool>             Wipe all ratings for a tool (destructive)
  stack create             Create a named skill stack
  stack list               List all skill stacks
  stack delete <name>      Delete a non-builtin stack
  stack import             Import all curated packages as built-in stacks
  org create               Create a new organisation profile
  org list                 List all organisations
  org set <org_id>         Set your active organisation
  purge-stale [days]       Flag skills unused for N days (default 90)
  auto-retire              Auto-retire skills with >50% error rate
  rebalance                Rebuild all routing scores from current data
```

---

## Sub-command: health

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" health
```
Print the output verbatim.

---

## Sub-command: retire \<tool_name\>

Confirm with the user before proceeding:
> "This will insert 5×1-star ratings for '<tool_name>', effectively retiring it from all future discovery and routing results. Proceed? [y/N]"

If confirmed:
```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" retire <tool_name>
```

---

## Sub-command: override \<tool_name\> \<score\>

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" override_rating <tool_name> <score>
```

---

## Sub-command: reset \<tool_name\>

Confirm with the user (destructive operation):
> "This will permanently delete ALL ratings for '<tool_name>'. It cannot be undone. Proceed? [y/N]"

If confirmed:
```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" reset_ratings <tool_name>
```

---

## Sub-command: stack create

Ask the user for:
1. Stack name (kebab-case, e.g. `my-frontend-stack`)
2. Display name (e.g. "My Frontend Stack")
3. Description (one sentence)
4. Skills to include (comma-separated list of skill/MCP names)
5. Org ID (optional — press Enter to leave personal)

Then run:
```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" create_stack <stack_name> "<display_name>" "<description>" '["skill1","skill2"]' [<org_id>]
```

---

## Sub-command: stack list [org_id]

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" list_stacks [<org_id>]
```
Print as a formatted table with stack name, skill count, org, and built-in flag.

---

## Sub-command: stack delete \<stack_name\>

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" delete_stack <stack_name>
```

---

## Sub-command: stack import

Imports all 6 curated packages as built-in skill stacks.
```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" import-packages
```

---

## Sub-command: org create

Ask the user for:
1. Org ID (lowercase kebab-case, e.g. `acme-corp`)
2. Org name (display name)
3. Admin email (optional)

Then run:
```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_org.py" create <org_id> "<org_name>" [<admin_email>]
```

Print the created org profile.

---

## Sub-command: org list

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_org.py" list
```

---

## Sub-command: org set \<org_id\>

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_org.py" set_current <org_id>
```

---

## Sub-command: purge-stale [days]

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" purge_stale [<days>]
```

---

## Sub-command: auto-retire

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" auto_retire
```
Print the list of retired skills and why.

---

## Sub-command: rebalance

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_admin.py" rebalance
```
Print confirmation and the number of scores updated.
