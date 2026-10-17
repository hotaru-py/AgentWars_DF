# System 3 notes: D Map + demo

Append-only. Only System 3 writes this file. Newest entries at the bottom.
Format: `- [YYYY-MM-DD HH:MM] <what changed / blocker / contract change>`
Tag contract changes with **CONTRACT** so the merge step surfaces them.

- [2026-10-17 13:20] Track D done: `dispatch_agent/map.html` (Leaflet 1.9.4 from cdnjs + OSM tiles, no key) and a "Dispatch Agent" section at the top of README.md (run steps, panel table, demo script). The old README content is kept below it as "Fallback demo". Tested in headless Chromium against fake states built with the real feedback.py: no JS errors, waits cleanly while state.json is missing.
- [2026-10-17 13:20] Map = live map + verification board: workflow stepper (Plan/Critic/Disrupt/Re-plan/Notify/Learn), acceptance checks computed in the browser (each order on exactly 1 van, capacity, broken van idle, 3 candidates scored, re-plan beats do-nothing, 1 message per affected order, <40 words, has ETA, ETA error falls on repeat), critic table, before/after cards, van table, notification cards, learning chart, tool-call timeline.
- [2026-10-17 13:20] **CONTRACT** (additive, all optional; for Track C): besides the required `depot, orders, vehicles, plan, disruption, policy{version,delay_factor,history}, notifications`, state.json may also carry the fields below. The map hides each panel when its field is missing, so nothing breaks if C skips them.
  - `notifications`: `[{"order_id", "message"}]` (an optional `"customer"` is also shown)
  - `critic`: the `feedback.critic()` return value as-is (`{best_id, verdict, table}`) from the latest plan_routes / replan_routes
  - `kpis`: `feedback.kpis()` of the applied plan
  - `baseline_plan`: the do-nothing Plan (`routing.retime(old_plan, disrupted_matrix)`), drawn dashed grey
  - `before_after`: `{"before": {total_min, total_km, late_orders}, "after": {...}}`, where before = do-nothing and after = applied plan
  - `affected_orders`: `["O6", "O10"]` (ETA changed > 5 min)
  - `last_run`: `{"eta_error", "changes"}` from `learn()` (shows a "rolled back" badge)
  - `events`: `[{"ts": unix_seconds, "tool": "replan_routes", "summary": "short text"}]`, one per tool call, append-only until reset
- [2026-10-17 13:20] Risk for Track B/C (found with fake data, not fixed, not my files): as the learned flood factor rises, predicted ETAs on the disrupted matrix get longer, so the plan's `overall` drops run after run; on the 3rd flood run `learn()` hit the "dropped two runs in a row" rule and **rolled back** (4.27 -> 4.59 -> back to 4.27). That breaks the 4.0 -> 4.5 -> 4.75 demo beat. Suggest scoring rollback on eta_error (or on overall under the realized ETAs) instead of the predicted-plan overall. Please check with the real routing.py.
