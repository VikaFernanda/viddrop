---
name: build-with-tests
description: Use this skill when implementing or extending a feature in 
Viddrop. Reads CLAUDE.md and the technical brief first, matches existing 
patterns (signals, workers, QSS themes), writes production code with unit 
tests alongside it, and runs ruff, mypy, and pytest at the end. 
Triggers on: "build", "implement", "add", "extend", "ship the feature".
---

Process:

1. Read CLAUDE.md so you know the project rules, stack, and security rules.
2. Read the technical brief so you stay inside its scope.
3. Look at 2-3 similar features in the codebase. Note:
   - How signals and slots are connected between core and UI.
   - How workers are created and how progress is reported.
   - How errors are handled and surfaced to the UI.
   - How credentials are handled (always via credential_store.py).
4. Implement the feature in the smallest coherent steps:
   - Write the core/backend code.
   - Write its unit test. Confirm it passes.
   - Write the UI code.
   - Write its pytest-qt test. Confirm it passes.
5. When the feature is complete, run:
   - `ruff check src/`
   - `mypy src/`
   - `python -m pytest`
6. Return a short summary:
   - Files changed
   - Signals and workers reused
   - Any new dependency added (flag it)
   - Any suggested CLAUDE.md rule addition

Security rules (non-negotiable):
- Never log credentials, tokens, cookies, or HTTP headers.
- Never write credentials to SQLite or plain files.
- Never pass credentials as visible CLI args — use yt-dlp config files in a secure temp dir.
- Never expose raw subprocess stderr directly to the user.
- Never hardcode colors — QSS files only.
