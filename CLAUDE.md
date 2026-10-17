# Team rules for Claude (all systems)

This repo is built in parallel on 3 systems and merged at the end.

## Before any work
1. Read `.system-id` in the repo root (untracked, local only) to learn which
   system you are (`system-1`, `system-2` or `system-3`). If it's missing,
   ask the user and create it. Never commit it.
2. Read `specs/CONTEXT.md` (shared state and decisions), then
   `specs/WORKBOARD.md` (tracks, file ownership, contracts).
3. Skim the other systems' notes in `specs/context/` for anything newer than
   `CONTEXT.md`'s "Merged through" line.

## While working
- Edit only the files your track owns (WORKBOARD.md, "Tracks" table).
- **Never edit `specs/CONTEXT.md` directly.** Write progress, blockers and
  contract changes as new lines at the bottom of **your own**
  `specs/context/<system-id>.md`, and only that file. Tag contract changes **CONTRACT**.
- Never commit, push, pull or merge. The humans handle git.

## Merging context
Run `/sync-context` after pulling, or whenever asked to "merge context". It
folds every system's new notes into `specs/CONTEXT.md`. Only the integrator
(system-1) should run it unless the user says otherwise.
