# System 1 notes: B Feedback + C Agent/tools (integrator)

Append-only. Only System 1 writes this file. Newest entries at the bottom.
Format: `- [YYYY-MM-DD HH:MM] <what changed / blocker / contract change>`
Tag contract changes with **CONTRACT** so the merge step surfaces them.

- [2026-10-17 12:50] Track B done: `feedback.py` + 12 tests. Starting Track C.
- [2026-10-17 12:55] Context protocol added: per-system notes in specs/context/, union merge in .gitattributes (tested), CLAUDE.md rules, /sync-context command. Each system must create its own untracked .system-id (system-2 / system-3).
- [2026-10-17 13:05] Track C written: `tools.py` (8 tools), `agent.py` (single ADK agent, gemini-3.8-flash + retry). Uses `_routing_fallback.py` (C-owned stand-in) until A's routing.py lands; switches automatically once `routing.build_graph()` stops raising NotImplementedError.
- [2026-10-17 13:05] **CONTRACT** (for A): `graph[a][b]` and `graph[b][a]` must be SEPARATE dicts. A shared dict gets multiplied twice by apply_disruption (bug I hit in the fallback: x16 not x4).
- [2026-10-17 13:05] **CONTRACT** (B, internal): `simulate_day` also returns `disrupted_orders` (orders whose trip crossed the disruption). `learn` rolls back when ETA error rises twice in a row (not when the plan score drops). NOISE is now +-3%.
- [2026-10-17 13:05] Offline flow verified: 3 flood runs give ETA error 15.8% -> 9.8% -> 4.2%, factor 4.0 -> 4.43 -> 4.70, ~140-160 min saved vs do-nothing. Breakdown: 3 late -> 0. 32 tests pass. Live adk run in progress.
- [2026-10-17 13:12] Live adk run OK: plan + flood end to end, 2x 429 retried fine, 3 messages 29-34 words. Added new_eta_clock (9:00 AM shift start) to replan_routes; agent now writes clock times in messages. Track C done pending merge with A's routing.py.
