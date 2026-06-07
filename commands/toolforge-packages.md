---
description: Browse and install curated ToolForge skill packages (best-for-business, best-for-coding, best-for-design, best-for-token-reduction, best-for-personal, best-for-testing). Run without arguments to see all packages with descriptions, or pass a package ID to install it.
---

You are running the `/toolforge-packages` command.

## Step 1: Parse the argument

The user may have typed:
- `/toolforge-packages` — list all available packages
- `/toolforge-packages <package_id>` — show details and offer to install a specific package
- `/toolforge-packages install <package_id>` — install a specific package immediately

Extract `<package_id>` from the command arguments if present.

## Step 2: List mode (no package_id given)

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_packages.py" list
```

Parse the JSON output and render a clean table:

```
========== ToolForge Curated Packages ==========

  best-for-business       6 tools   Business / SaaS engineering teams
  best-for-coding         6 tools   Core developer toolkit
  best-for-design         5 tools   Frontend UI / component design
  best-for-personal       5 tools   Solo builders and side projects
  best-for-testing        5 tools   QA / E2E testing stack
  best-for-token-reduction 4 tools  Cut API costs without losing capability

Run /toolforge-packages <package_id> to see details and install.
=================================================
```

Stop here if no package_id was given.

## Step 3: Detail + install mode (package_id given)

Shell out to:

```
python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_packages.py" render <package_id>
```

Print the rendered output verbatim.

Then ask the user:

> "Install all tools in this package? [Y/n]"

If the user confirms (Y or Enter):

1. Shell out to get install commands:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_packages.py" install_commands <package_id>
   ```

2. For each install command returned, run it sequentially. Before running each command, validate it through the ToolForge install sandbox:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_install.py" validate "<command>"
   ```
   If validation fails, skip that tool and note it. If validation passes, run the install command.

3. After all installs, log each successfully installed tool:
   ```
   python "${CLAUDE_PLUGIN_ROOT}/bin/toolforge_db.py" log_install <tool_name> ui 1
   ```

4. Print a summary: how many installed, how many skipped, and any tools requiring API keys that were not configured.

## Step 4: Suggest next steps

After install, print:
> "Run /toolforge-status to see your updated tool ratings and health."
> "Run /toolforge-rescan to rebuild the router index with the new tools."
