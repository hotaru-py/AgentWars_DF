# How to run the Dispatch Agent

About 5 minutes from a fresh clone. You need **Python 3.10 or newer**, an internet
connection (for the map and Gemini), and a free **Gemini API key** (you can paste it
in the app; see step 2).

## 1. Install (once)

**Windows (PowerShell)**

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Start

From the repo root:

```bash
python run.py
```

Your browser opens the **Dispatch Console** at
http://localhost:8001/console.html?api=http://localhost:8000.

- **No key yet?** A **Connect Gemini** box appears. Get a key at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (**Create API
  key**), paste it, and click **Save and start**. The app checks the key with
  Google, saves it to `dispatch_agent/.env` on your machine, and restarts itself.
- **Already have a key in `dispatch_agent/.env`?** No box; go straight to step 3.

To stop, press **Ctrl+C** in the terminal. To start fresh and forget what the agent
learned, run `python run.py --fresh`.

## 3. Try it (2 minutes)

Use the buttons at the bottom of the map, or type in the chat (bottom right).

| Do this | What you should see |
|---|---|
| Click **Plan routes** | Three coloured van routes appear on real streets. The left panel compares 3 plans and shows total driving time. Every delivery is on time. |
| Click **🌊 Flood** (or type *"Heavy rain flooding Ashok Nagar"*) | A red zone appears. The agent re-plans, routes bend around the zone, affected customers' pins pulse, and a "customers notified" pop-up shows their messages. |
| Read the chat | A plain-English summary: what happened, what it did, what it learned. |
| Click **↺ Reset**, then **🌊 Flood** again (twice) | The **Getting smarter** chart drops: the agent's delay estimate gets closer each time (about 22% → 14% → 6% off). |
| Type any problem, e.g. *"CM convoy causing traffic in Tambaram"* or *"Van V1 broke down"* | Works for about 40 Chennai areas and roads, by name. |
| Click **</>** (top right) | Developer view: every model and tool call with raw inputs and outputs, token use, raw state, and the scoring tables. |

## If something goes wrong

| Problem | Fix |
|---|---|
| "Port 8000 is already in use" | Another copy is running. Close it, or run `API_PORT=8100 UI_PORT=8101 python run.py` (PowerShell: `$env:API_PORT=8100; $env:UI_PORT=8101; python run.py`). |
| The chat says the key was rejected | Click **</>** → **Tokens** → **Change API key**, and paste a working key. |
| The agent stops replying, or the chat shows a 429 / quota error | The free Gemini tier allows about 20 requests per day per model, and one problem uses about 5. Set another model in `dispatch_agent/.env`, e.g. `GEMINI_MODEL=gemini-3.5-flash`, then restart, or use a key with billing enabled. |
| The map is empty | Normal before the first action: click **Plan routes**. |
| "google-adk is not installed" | Activate the venv (step 1) and run `pip install -r requirements.txt`. |
