# Contributing to ToolForge

Thanks for your interest in improving ToolForge. This document describes how to contribute productively without breaking the v1 scope or the security model.

## 1. Scope

v1 is locked to the demo target. PRs that add categories beyond `UI, backend, database, testing, devops`, replace the Bayesian ranker with ML, add cloud sync, add a TUI, or add a daemon will be deferred to v0.2+.

Bug fixes, security hardening, doc improvements, and fallback-entry refreshes are always welcome.

## 2. Local development

- Python 3.10+, stdlib only. No `requirements.txt`. Hook scripts are UV single-file format.
- Install for local development:
  ```
  claude plugin marketplace add ./toolforge && claude plugin install toolforge@local-toolforge
  ```
- Reset state between tests:
  ```
  rm ~/.claude/toolforge.db
  ```
  (or back it up first if you have real data).

## 3. Updating fallback entries

When refreshing `fallback/<category>.json`:

- Verify every URL is reachable and every install command runs cleanly on a fresh machine.
- Re-generate the SHA-256 manifest:
  ```
  cd toolforge && sha256sum fallback/*.json > fallback/manifest.sha256
  ```
  (or the Python `hashlib` equivalent on Windows).
- The integrity check at runtime will refuse to load fallback data if the manifest mismatches. This is a security feature, not a bug.

## 4. Security PRs

Any change to `toolforge_validate_url.py`, `toolforge_install.py`, or the SKILL.md allow-list / deny-list arrays requires explicit reviewer sign-off. These are the security boundary. Bypasses are deal-breakers.

## 5. Coding standards

- Functions under 30 lines where possible
- Type hints on every Python signature
- No dependencies outside Python stdlib
- One concern per file
- Errors fail loud (exit non-zero, message on stderr); never silent-skip a real failure
- No em dashes anywhere in code, comments, markdown, or skill prompts (project-wide rule)

## 6. Pull-request checklist

- [ ] `toolforge_db.py status` runs cleanly against a fresh DB
- [ ] `/toolforge UI` returns 5 entries (live or fallback)
- [ ] Fallback integrity check still passes after fallback JSON changes
- [ ] No new dependencies added to any script
- [ ] No em dashes introduced
- [ ] Changelog entry added under Unreleased

## 7. Reporting security issues

Email `28gugales@gmail.com` privately. Do not file a public issue for security regressions in the allow-list logic.
