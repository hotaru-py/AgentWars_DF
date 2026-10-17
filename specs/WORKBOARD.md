# Workboard: parallel tracks

Each track owns **only its files**, so merging means copying files over, with no
conflicts. Code against the contracts below, not against each other's
half-finished code. If you must change a contract, add a **CONTRACT** line to your own
`specs/context/<system-id>.md` and tell the others.

All code lives in `dispatch_agent/` (a new ADK app next to the old
`logistics_agent/`). Everything is plain dicts and lists so it serializes to
JSON for ADK tool results and `out/state.json`.

## Timeline (T = start)

| Time | What |
|---|---|
| T+0 to T+25 | Tracks A, B, C, D in parallel. A and B unit-test their own functions |
| T+25 | **Merge.** Copy every track's files onto the Track C system |
| T+25 to T+35 | Track C integrates and runs the demo prompts in `adk web` |
| T+35 to T+45 | Rehearse the demo script below twice. Freeze |

## Tracks

| Track | Owns these files | Done when |
|---|---|---|
| **A Routing** | `dispatch_agent/routing.py`, `tests/test_routing.py` | Dijkstra, the matrix and `solve()` pass tests; flood changes at least 1 route |
| **B Feedback** | `dispatch_agent/feedback.py`, `tests/test_feedback.py` | `kpis`, `critic`, `simulate_day` and `learn` pass tests; 3 learn steps move the flood factor 4.0 to about 4.5 to about 4.75 |
| **C Agent + tools** (integrator) | `dispatch_agent/tools.py`, `dispatch_agent/agent.py`, `dispatch_agent/__init__.py` | The demo script runs end to end in `adk web` |
| **D Map + demo** | `dispatch_agent/map.html`, `README.md` (dispatch section), demo script rehearsal | The map shows pins, colored routes and the disruption circle from `out/state.json`, refreshing every 2 s |

Shared and read-only (already written): `dispatch_agent/seed.py`, `dispatch_agent/geo.py`
(`haversine_km`, `road_km(a, b)`, `km_to_min`, `edge_disrupted(a, b, disruption)`, `NODES`).
**A and B must both use `geo.py` for distances and circle tests**; don't reimplement them.

**Isolation:** A and B never import each other. B tests with hand-built plans,
matrices and graphs that follow the shapes below. C imports both, so until they
land, C codes against the contracts and smoke-tests with the stubs replaced by
quick fakes.

## Contracts

### Ids and seed (`seed.py`, done)

- Node ids: `"DEPOT"`, `"O1"` to `"O10"`. Vehicle ids: `"V1"` to `"V3"`.
- `DEPOT = {"id", "area", "lat", "lng"}`
- `ORDERS = [{"id", "customer", "phone", "area", "lat", "lng", "deadline_min", "priority": "normal"|"urgent", "weight_kg"}]`
- `VEHICLES = [{"id", "driver", "capacity_kg", "status": "active"|"broken_down"}]`
- `SCENARIOS = {"flood"|"traffic"|"breakdown": {"kind", "label", "center": [lat, lng] | None, "radius_km", "vehicle_id" | None}}`
- `SPEED_KMPH = 25`, `ROAD_FACTOR = 1.3`

A **disruption** is a scenario dict plus `"multiplier": float`. The *predicted*
multiplier comes from the policy; the *true* one lives only in `feedback.TRUTH`.

### Track A: `routing.py`

```python
Graph = dict[str, dict[str, dict]]   # graph[a][b] = {"min": float, "km": float}, undirected

def build_graph(k: int = 4) -> Graph
    # nodes = DEPOT + ORDERS; connect each node to its k nearest (haversine*ROAD_FACTOR km,
    # minutes = km / SPEED_KMPH * 60); make it undirected and make sure it is connected.

def apply_disruption(graph: Graph, disruption: dict) -> Graph
    # returns a COPY. flood/traffic: multiply "min" on every edge with an endpoint or its
    # midpoint inside the circle by disruption["multiplier"]. breakdown: graph unchanged.

def dijkstra(graph: Graph, a: str, b: str) -> dict
    # {"path": [node ids], "min": float, "km": float}

def cost_matrix(graph: Graph) -> dict[str, dict[str, dict]]
    # all-pairs dijkstra: m[a][b] = {"min", "km", "path"}

def solve(matrix, vehicles: list[dict], params: dict) -> dict   # returns a Plan
    # cheapest insertion; skips vehicles with status "broken_down"; respects capacity_kg.
    # params = {"name": str, "late_penalty": float, "balance_weight": float}
    # cost = total_min + late_penalty * minutes_late (x3 for urgent) + balance_weight * (max_route_min - min_route_min)

def retime(plan: dict, matrix: dict) -> dict
    # returns a COPY of plan with the SAME stop order, but path / eta_by_order / duration_min /
    # distance_km recomputed on `matrix`. C uses it for the "do nothing" baseline after a disruption.
```

**Plan** (shared by everyone):

```python
{"id": "plan-<n>", "params": {...},
 "routes": [{"vehicle_id": "V1", "stops": ["O4", "O3"],          # order ids, in order
             "path": ["DEPOT", "O4", "O3"],                       # full node path incl. dijkstra intermediates
             "eta_by_order": {"O4": 12.5, "O3": 27.0},             # minutes from shift start
             "duration_min": 27.0, "distance_km": 9.1}],
 "unassigned": []}
```

### Track B: `feedback.py`

```python
TRUTH = {"flood": 5.0, "traffic": 3.0, "breakdown": 1.0}   # hidden from the agent; +-10% noise, seed 42
DEFAULT_POLICY = {"version": 1,
  "delay_factor": {"flood": 4.0, "traffic": 2.5, "breakdown": 1.0},
  "candidates": [{"name": "balanced", "late_penalty": 1.0, "balance_weight": 0.5},
                 {"name": "deadline-first", "late_penalty": 4.0, "balance_weight": 0.1},
                 {"name": "compact", "late_penalty": 0.5, "balance_weight": 0.0}],
  "history": []}   # [{"run": n, "kind": str, "eta_error": float, "overall": float}]

def kpis(plan: dict, orders: list[dict], matrix) -> dict
    # {"on_time": % orders ETA <= deadline, "efficiency": min(1, mst_km / plan_km),
    #  "cycle": min(1, mean direct depot->order min / mean ETA), "balance": 1 - (max-min)/max route_min,
    #  "resilience": on_time with every leg x1.2, "overall": weighted mean}
    # each value 0..1, higher is better. weights: 0.30, 0.20, 0.15, 0.15, 0.20

def critic(plans: list[dict], orders, matrix, baseline: dict | None = None) -> dict
    # {"best_id": str | None, "verdict": "apply"|"hold",
    #  "table": [{"plan_id", "name", **kpis}]}   # hold if best overall < baseline overall;
    # discard plans with unassigned orders

def simulate_day(plan: dict, base_graph: Graph, disruption: dict | None, run: int) -> dict
    # base_graph = the UNDISRUPTED graph (C passes routing.build_graph(); B tests with a hand-made one).
    # walks each route.path leg by leg: base min from base_graph[a][b]["min"]; if
    # geo.edge_disrupted(a, b, disruption) multiply by TRUTH[kind] * seeded noise for this run ->
    # {"realized_eta": {order_id: min},
    #  "observed_ratio": float}   # realized min / undisrupted min, over the disrupted legs only
    #                             # (what a real fleet would measure; never expose TRUTH itself)

def learn(policy: dict, kind: str, predicted_eta: dict, realized_eta: dict,
          observed_ratio: float, overall: float) -> dict
    # eta_error = mean(|pred - real|) / mean(real)
    # new = 0.5*old + 0.5*observed_ratio, clamped to +-25% of old  (4.0 -> ~4.5 -> ~4.75)
    # returns {"policy": new_policy (version+1, history appended), "changes": {...}, "eta_error": float}
    # rollback: if overall dropped two runs in a row, return the previous policy instead

def load_policy() -> dict / def save_policy(policy: dict) -> None
    # dispatch_agent/out/policy.json; save atomically (write tmp, then os.replace)
```

### Track C: `tools.py` and `agent.py`

The tools hold the world state in a module-level dict and write
`dispatch_agent/out/state.json` after every change (for the map).
Every tool returns `{"status": "ok"|"error", ...}`, and a plan or order's own
state never uses the key `"status"`.

| Tool | Does |
|---|---|
| `get_world_state()` | Orders, vehicles, current plan summary, active disruption, policy version |
| `plan_routes()` | Builds 3 candidates (policy params), runs the critic, applies the best. Returns the candidate table and per-van summary |
| `apply_disruption(scenario: str)` | `"flood"`, `"traffic"` or `"breakdown"`. Uses the **policy** delay_factor as the predicted multiplier |
| `replan_routes()` | 3 candidates on the disrupted graph, critic against the do-nothing plan. Returns before/after (total min, km, late orders) and `affected_orders` (ETA changed > 5 min) |
| `notify_customers(messages: list[dict])` | `[{"order_id", "message"}]`. Rejects messages over 40 words. Records them as sent |
| `run_day()` | `simulate_day` + `learn` + `save_policy`. Returns eta_error, the policy change and the history |
| `get_policy()` | Current policy (read-only) |
| `reset(hard: bool = False)` | Reloads the seed; `hard=True` also resets the policy |

Agent instruction (core): *autonomous dispatcher, no human approval. On a
disruption, always: apply_disruption, then replan_routes, then
notify_customers (one message per affected order, under 40 words, including the
new ETA), then run_day. End with a 3-line summary: time saved, customers
notified, and what was learned (old and new delay factor, ETA error).*
Model: `gemini-3.8-flash`, with the retry options copied from `logistics_agent/agent.py`.

### Track D: `map.html`

A single static file, Leaflet from cdnjs, OSM tiles. Polls `out/state.json`
every 2 s. Shows the depot, numbered order pins (urgent in red), one colored
polyline per van through `route.path` node coordinates, and the disruption as a
translucent red circle. Serve it with `python -m http.server 8001 -d dispatch_agent`.

`out/state.json` shape: `{"depot", "orders", "vehicles", "plan", "disruption", "policy": {"version", "delay_factor", "history"}, "notifications": [...]}`

## Demo script (2 minutes)

1. `adk web` opened, with the map in a second window. Prompt: **"Plan today's routes."** The critic table shows 3 candidates and the best is applied. Routes appear on the map.
2. **"Flooding on OMR!"** The tool calls stream: apply_disruption, replan_routes, notify_customers, run_day. The map reroutes around the red circle. The agent summarizes time saved and customers notified.
3. **Feedback beat:** run `reset` (soft, so the policy is kept), then "Flooding on OMR!" twice more. ETA error falls (about 20%, then about 10%, then about 5%) and the delay_factor goes 4.0, then about 4.5, then about 4.75. No human approved anything.
