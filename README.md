# Logistics Dispatcher: Google ADK demo

## Dispatch Agent (main demo, `dispatch_agent/`)

An autonomous Chennai delivery dispatcher on Google ADK: 1 depot, 10 orders,
3 vans. It plans routes, takes a disruption (flood on OMR, traffic jam in
T. Nagar, van breakdown), re-plans, notifies the affected customers, then
simulates the day and learns a better delay factor. No human approves anything.

### Run it (two terminals, from the repo root)

```bash
cp logistics_agent/.env.example dispatch_agent/.env   # then set GOOGLE_API_KEY
adk web --port 8000                                   # pick "dispatch_agent"
python -m http.server 8001 -d dispatch_agent          # map: http://localhost:8001/map.html
```

The map polls `dispatch_agent/out/state.json` every 2 s, which the agent's tools
rewrite after every change. Open it through the server above, not as a file.
`?src=out/other.json` points it at a different state file.

### What the map shows, and what it verifies

| Panel | Shows | Verifies |
|---|---|---|
| Map | Depot, numbered order pins (red = urgent, red ring = late, pulsing = affected), one colored route per van, the do-nothing plan dashed, the disruption circle | Routes go around the disruption |
| Metric cards | Total drive time, distance, late orders, orders assigned; delta vs the do-nothing plan after a re-plan | Before vs after |
| Agent workflow | Plan, Critic, Disrupt, Re-plan, Notify, Learn; each step lights up when its result is in `state.json` | The full loop ran with no human step |
| Acceptance checks | Every order on exactly one van, capacity, broken van idle, 3 candidates scored, re-plan beats do-nothing, one message per affected order, messages under 40 words and include the ETA, ETA error falls on a repeat run | The spec's acceptance criteria, computed live in the browser |
| Critic | The 3 candidate plans on 5 KPIs, the applied one highlighted, or "hold" | The code critic, not the LLM, picks the plan |
| Vans | Stops in order with ETAs (late in red), minutes, km, load/capacity | Capacity and deadlines |
| Notifications | One card per message, with its word count | Customer communication |
| Learning loop | Delay factors, policy version, ETA error per simulated day | Self-improvement across runs |
| Tool calls | Timeline of the agent's tool calls | Agentic loop (also in the adk web panel) |

Panels that need optional `state.json` fields (critic, before/after, affected
orders, do-nothing plan, events) stay hidden until the tools write them.

### Demo script (2 minutes)

1. `adk web` in one window and the map in a second. Prompt **"Plan today's routes."** The critic table shows 3 candidates, the best is applied and 3 routes draw on the map.
2. **"Flooding on OMR!"** The tool calls stream: apply_disruption, replan_routes, notify_customers, run_day. The red circle appears, routes redraw around it, the affected pins pulse and notification cards fade in. All acceptance checks are green.
3. **Feedback beat:** `reset` (soft, keeps the policy), then "Flooding on OMR!" twice more. The ETA error bars shrink and the flood delay factor climbs. No human approved anything.

## Fallback demo: Logistics Dispatcher (`logistics_agent/`)

A conversational supply-chain agent for a South India warehouse network, built
with Google's Agent Development Kit (Python). It checks stock, finds shortages,
plans road routes, tracks shipments and moves stock between warehouses after
the user confirms.

```
logistics_dispatcher (root_agent)
├── inventory_agent   (AgentTool)  get_stock, find_low_stock
├── routing_agent     (AgentTool)  shortest_route (Dijkstra over the road graph)
├── get_shipment_status
└── create_transfer   (called only after the user says yes)
```

All data is mock and in memory ([logistics_agent/data.py](logistics_agent/data.py)).
Restart the process to reset it.

## Setup

Requires Python 3.10+. On Windows (PowerShell):

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy logistics_agent\.env.example logistics_agent\.env
```

On macOS/Linux:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp logistics_agent/.env.example logistics_agent/.env
```

Then open `logistics_agent/.env` and set `GOOGLE_API_KEY` to a Gemini key from
[Google AI Studio](https://aistudio.google.com/apikey). `.env` is git-ignored.
Never commit it.

## Run

From the repo root (the folder that contains `logistics_agent/`):

```bash
adk web --port 8000      # browser UI at http://localhost:8000, pick "logistics_agent"
adk run logistics_agent  # or chat in the terminal
```

The first `adk` command asks whether to enable telemetry. Answer it once
before the demo (or run `adk telemetry disable`) so it doesn't interrupt you
on stage.

### Model and rate limits

The model is set by `MODEL` in [logistics_agent/agent.py](logistics_agent/agent.py)
(`gemini-3.8-flash`; `gemini-2.5-flash` returns 404 for new AI Studio keys).
A free-tier key allows only **5 requests per minute per model**, and one
"fix the shortage" turn makes about 7 model calls, because every sub-agent hop
is its own call. The agent retries 429s with backoff, so the turn still
finishes, but it can take about a minute. For a smooth live demo, enable
billing on the AI Studio project, which raises the limits a lot.

## Demo prompts

1. **"Which warehouses are low on stock?"** The inventory agent lists every
   warehouse/SKU at or below 10 units, in a table.
2. **"Madurai has no pumps. Fix it."** The dispatcher finds a surplus warehouse,
   gets the route and ETA, and proposes a transfer. Reply **"yes"** to create
   the shipment and deduct the stock.
3. **"What's up with SHP-1002?"** It reports 50 sensors delayed at Coimbatore
   because of a highway closure.

## Tests

```bash
pytest -q
```

The tests cover every tool and make no network or LLM calls.

## Swapping in the Google Maps Routes API

`shortest_route(origin, destination)` keeps a stable signature. Set
`USE_ROUTES_API=TRUE` to send it through `_routes_api()` in
[logistics_agent/tools.py](logistics_agent/tools.py). That function is
currently a marked stub that returns an error. When it is unset, the local
Dijkstra graph is used.
