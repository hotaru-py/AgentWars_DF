# Shared context (merged; read first)

**Don't edit this file directly.** Each system appends notes to its own
`specs/context/<system-id>.md`, and `/sync-context` (run by system-1) merges
them in here. That way pulls never conflict and nothing gets overwritten. See
`CLAUDE.md` for the full protocol. The work split and interfaces are in
[WORKBOARD.md](WORKBOARD.md). The original long spec is
[1230-spec.md](1230-spec.md); where they disagree, **this file and WORKBOARD.md win**.

**Merged through:** system-1: 2026-10-17 12:50 · system-2: none · system-3: none

## The build in one paragraph

A Dispatch Agent for Chennai deliveries: 1 depot, 10 orders, 3 vans. The ADK
agent plans routes, takes a disruption (flood on OMR, traffic jam in T. Nagar,
or van breakdown), re-plans, and notifies the affected customers. A
deterministic critic scores 3 candidate plans on 5 KPIs and applies the best
one. A simulated day then reveals the "true" delay, and the learner updates the
policy, so ETA error drops on every repeat run. No human approves anything.

## Decisions (45-minute build)

| # | Decision | Why |
|---|---|---|
| D1 | 45 min total: about 25 min build, 10 min merge, 10 min rehearsal | Hard clock |
| D2 | **ADK** agent loop (not a hand-rolled loop) | Already working live; tool schemas and 429 retry come free |
| D3 | **`adk web` is the UI.** The tool-call panel is the agent log | No frontend build needed |
| D4 | Optional Leaflet `map.html` (OSM tiles, no key) reads `dispatch_agent/out/state.json` | Visual bonus that blocks no one |
| D5 | No Google Maps API. Travel time = haversine × 1.3 road factor at 25 km/h | No key setup, deterministic, works offline |
| D6 | Sparse graph (each node to its 4 nearest), then Dijkstra | On a complete graph, Dijkstra just returns the direct edge |
| D7 | The VRP consumes the **all-pairs Dijkstra matrix**, recomputed after each disruption | Otherwise disruptions don't affect the re-plan |
| D8 | VRP in pure Python (cheapest insertion), not OR-Tools | No new dependency; 10 orders is trivial |
| D9 | Flood and traffic are **circles** (center + radius); edges touching a circle are slowed | Easy to draw; real routes go around them |
| D10 | Feedback loop is slim: 5 KPIs on the plan (not a 5×5 grid), 3 candidates, learner tunes `delay_factor` only | Keeps the pitch, fits the clock |
| D11 | The agent writes customer messages itself in one `notify_customers` call (batch) | No extra Gemini calls (free tier is **5 requests/min**) |
| D12 | No Jev, no reflection loop, no CSV upload, no natural-language order add | Out of time |
| D13 | Keep `logistics_agent/` untouched as the fallback demo | Already works live |
| D14 | Claude instances do **not** commit or push. Humans handle git | Team rule |

## Environment and gotchas

- Python 3.11 venv: `py -3.11 -m venv .venv; .venv\Scripts\Activate.ps1; pip install -r requirements.txt`
- Key: copy `logistics_agent/.env.example` to `<agent folder>/.env` and set `GOOGLE_API_KEY`. Never commit it.
- Model: **`gemini-3.8-flash`**. `gemini-2.5-flash` returns 404 for new AI Studio keys.
- Free tier allows **5 requests/min per model**. One disruption run makes about 5 model calls. Billing plan: stay on the free tier; enable billing only if we hit 429s while running.
- Never put an `agent.py` in the repo root: ADK then goes into single-agent mode and hides every agent folder.
- Never use `:` in filenames, because Windows can't check them out.
- Run `adk web --port 8000` from the repo root and pick `dispatch_agent` in the dropdown.
- The first `adk` command asks about telemetry. Answer it once before the demo.

## Status

| Track | Owner/system | Status |
|---|---|---|
| A Routing | System 2 | not started |
| B Feedback | System 1 (with C) | **done**: 12 tests pass |
| C Agent + tools | System 1 (integrator) | not started |
| D Map + demo | System 3 | not started |

## Log

- 2026-10-17: Plan reset for a 45-min build. Seed data written in `dispatch_agent/seed.py`. Skeleton stubs created for every track.
- 2026-10-17: 3 systems. System 1 = B + C (integrator), System 2 = A, System 3 = D. Added shared `dispatch_agent/geo.py` (distances + `edge_disrupted`); A and B must use it. `simulate_day` now takes `base_graph` (see WORKBOARD).
- 2026-10-17: Track A contract gained `retime(plan, matrix)` (same stops, recomputed ETAs) for C's do-nothing baseline.
- 2026-10-17: Track B done (`feedback.py`, 12 tests). kpis() also returns late_orders, total_min, total_km for C's before/after cards. Learner verified: flood 4.0 -> 4.5 -> 4.75 -> 4.875, ETA error falls each run.
