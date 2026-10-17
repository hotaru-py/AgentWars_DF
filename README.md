# Logistics Dispatcher: Google ADK demo

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
