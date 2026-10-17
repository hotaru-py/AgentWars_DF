---
description: Merge every system's notes (specs/context/*.md) into specs/CONTEXT.md without losing anything
---

Merge the per-system context notes into the shared context file. Do not run
any git command that changes state (no commit, pull, merge, stash or checkout);
read-only commands like `git status` and `git diff` are fine.

1. Read `specs/CONTEXT.md` and find its **Merged through** line, which records
   the timestamp of the last merged entry per system.
2. Read every `specs/context/system-*.md`. Collect the entries newer than that
   system's merged-through timestamp. If `git status` shows conflict markers
   (`<<<<<<<`) in any context file, keep **both** sides' lines, remove the
   markers, and tell the user.
3. Update `specs/CONTEXT.md`:
   - **Status** table: set each track's status from its system's latest entries.
   - **Decisions**: add a row for any entry that records a new decision.
   - **CONTRACT** entries: list them under the Log and also make sure
     `specs/WORKBOARD.md` reflects them. If two systems changed the same
     contract in incompatible ways, don't pick one. Stop and ask the user.
   - **Log**: append each new entry as `- [time] system-N: <entry>`, oldest first.
   - **Merged through**: update to each system's newest merged timestamp.
4. Never delete or rewrite existing Log lines or other systems' notes files.
5. Reply with a short summary: new entries per system, status changes, any
   CONTRACT changes, and anything that needs a human decision.

$ARGUMENTS
