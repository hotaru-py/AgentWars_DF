# System 2 notes: A Routing

Append-only. Only System 2 writes this file. Newest entries at the bottom.
Format: `- [YYYY-MM-DD HH:MM] <what changed / blocker / contract change>`
Tag contract changes with **CONTRACT** so the merge step surfaces them.

- [2026-10-17 13:20] Track A done: `routing.py` (build_graph, apply_disruption, dijkstra, cost_matrix, solve, retime) + `tests/test_routing.py` (13 tests; with feedback tests 25 pass). Contract unchanged. Notes for C: plans include a route for every ACTIVE van (empty `stops: []`, path ["DEPOT"]) and none for broken vans; `duration_min` = last ETA (no return leg). Seed flood demo: base 194 min, do-nothing under flood 433 min (1 late), replan 295 min (1 late).
- [2026-10-17 13:35] **CONTRACT** (ack system-1): graph[a][b] and graph[b][a] are SEPARATE dicts (never shared), so a disruption multiplier applies exactly once per direction. Already true in routing.py (build_graph, apply_disruption); covered by test_apply_disruption_copies_and_slows_flooded_edges. Hand-built graphs in tests should follow the same rule.
