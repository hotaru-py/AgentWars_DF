# Dispatch v2: game plan to the end

Draft for review · 2026-10-20 · companion to [v2-spec.md](v2-spec.md) (the spec says *what*; this says *in what
order, and what has to be decided*).

## Where we stand

| Phase | State | What's missing against its "done when" |
|---|---|---|
| P1 Foundation | Built | The 50 orders × 5 vans < 5 s benchmark was never run (10 × 3 takes 3.5 s on OSRM). Console is plain HTML, not React (deliberate so far) |
| P2 Decision layer | Built | No labelled incident set, so "≥90% triage accuracy" is unmeasured. Outcomes are never written back, so calibration stays at identity. No automation-rate / wrong-auto-action report. Jev shadow results are logged but there's no comparison report. Live AI triage unverified (daily quota) |
| P3 Failed-delivery prevention | Done | Met on the twin (28% fewer failures). Trained model built, gated by the check |
| P4 Addresses | Not started | Orders still need lat/lng to be planned |
| P5 ETA + twin | Partly (day replay + learner) | P90 is a placeholder, no quantile model, no replay reports, no threshold tuning |
| P6 Driver + customer | Not started | Driver actions exist only as API endpoints and console test buttons |
| P7 Scale-out | Not started | |

## Decisions needed from you

Each has my recommendation; the plan below assumes it.

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | **`RISK_HOLDOUT`**: deliberately not messaging a share of risky customers so a trained model gets unbiased data | 0 (never) · 5% · 10% | **0 now; 5% once real deliveries flow**, reviewed after 3 months. On the twin, 10% cost a few failures and the model needed ~60 days of data to sometimes beat the rules. With no real traffic it buys nothing. Revisit when `/ml/status` shows ≥500 labels |
| D2 | Labelled data for the accuracy targets (P2 incidents, P4 addresses) | Write them ourselves · collect from a partner · synthetic only | **Write 100 incident reports + 200 Chennai addresses ourselves** (English/Tamil/Hindi mix), checked in under `tests/data/`. Synthetic-only can't support an accuracy claim |
| D3 | Gemini for real use | Free tier · billing on one project | **Billing**, with a monthly cap. The free tier (≈20 requests/day/model) can't run the P2 accuracy eval (100 reports × up to 5 samples) |
| D4 | Front end for P6 (driver app, tracking page) | Keep plain HTML pages · React + Vite + TS (spec) | **React + Vite + TS for P6 only**; keep `console.html` until it hurts. A driver app with offline queueing and GPS is past what one HTML file should hold |
| D5 | Where it runs | Local demo only · one VM (Docker Compose) · managed (Cloud Run + Cloud SQL) | **One VM with Compose** first: it's what `infra/` already describes. Managed later if traffic needs it |
| D6 | Messaging provider (P7) | Meta WhatsApp Cloud API directly · a BSP (Gupshup, Twilio, Karix…) · SMS only | Decide at P7 start; **leaning WhatsApp Cloud API direct** (no middleman fee) with SMS fallback. Needs a business account and approved templates, which takes weeks, so start the paperwork early |
| D7 | Jev | Keep optional, shadow only · drop | **Keep optional**; promote only if the comparison report (step 0) shows it beats the LLM on our labelled set |

## The plan

Order: close the gaps that make claims unmeasurable → addresses (needed for real orders) → honest ETAs →
driver app → production hardening → real providers. Sizes: S ≈ a session, M ≈ 2–3, L ≈ 4+.

### Step 0 · Close P1/P2 gaps (M)

1. Benchmark: a generated 50-order × 5-van day, planned on OSRM, asserted < 5 s in an opt-in test.
2. Write outcomes back so calibration can learn (spec §7.3): incident triage → `correct` / `incorrect` when
   a person overrides or closes it as not real; reply intent → when a person resolves differently.
3. Labelled incident set (D2) and an eval command (`python -m dispatch.eval triage`) that runs the default
   chain and any shadow provider, reports kind accuracy, automation rate and wrong-auto-action rate, and
   caches LLM answers so reruns cost nothing. This is also the Jev comparison report (D7).
4. Ledger report in the console: automation rate and wrong-auto-action rate per decision type.
5. Verify live AI triage and settle logprobs vs sampling (needs D3).

**Done when** P1 and P2's "done when" columns are all measured and met (or the spec says why not).

### Step 1 · P4 Addresses (L)

1. `address/normalize.py`: abbreviations ("opp", "nr", "st", "rd"), PIN, landmark phrases ("near X temple",
   "behind Y"), Tamil/Hindi transliterations of common words.
2. `address/digipin.py`: port of India Post's encoder/decoder (Apache-2.0, keep the notice), tested against
   their reference outputs.
3. Geocoder port with a chain: cache → gazetteer (`places.py`, extended with landmarks) → OSM Nominatim
   (self-hosted in Compose, or the public one at ≤1 request/s) → optional Google/Mappls behind a key.
4. `address_parse` decision (spec §7.1): rules (string similarity + geocoder score) → LLM → human.
5. "Drop a pin": below the gate, the confirmation message asks for a location pin or DIGIPIN, and the reply
   handler accepts it.
6. `geo_confidence` from this pipeline feeds the risk score (it already reads it).

**Done when** ≥85% automatic and median error < 150 m on the 200-address set (D2).

### Step 2 · P5 Honest ETAs and twin reports (L)

1. Real detours: incidents become OSRM segment-speed updates (or an avoid-area re-route), not the
   straight-segment approximation. Learn `OSRM_TIME_FACTOR` per zone and hour from the twin, and later
   from `stop.arrived`.
2. ETA model: baseline = OSRM × learned factors; then quantile regression (P50/P90) on `stop.arrived`
   (needs LightGBM, the first heavy dependency; or quantile linear models in numpy). Replace the P90
   placeholder.
3. Promise policy: the window shown to the customer is chosen from the P50/P90 range for a target hit rate.
4. Replay reports: `POST /twin/replay` (now 501) runs a day under a policy and stores a report. Every policy
   or threshold change ships with one (spec U6).
5. Threshold tuning: gate thresholds and `RISK_CONFIRM_THRESHOLD` swept on the twin, with results in the report.

**Done when** ≥85% of simulated deliveries land inside the promised window, with reliability plots per decision type.

### Step 3 · P6 Driver app and tracking page (L, front end per D4)

1. Driver app (mobile web, installable): today's stops in order, "call first" flags, navigation hand-off
   to Google Maps, arrive / deliver / fail with GPS and time at the door, photo upload, and offline queueing.
2. Driver identity: a per-driver login (magic link or PIN). This is the first real auth (see step 4).
3. Customer tracking page: a link in messages; live ETA window, "reschedule" and "share location" actions
   feeding the same reply handling.
4. POD photo check behind a flag (spec §7.1), off by default.
5. Twin drivers: simulated drivers use the same API flows, so a full day runs end to end.

**Done when** a full twin day runs through the real app flows.

### Step 4 · Production hardening (M; not in the spec yet, should be added)

1. Auth and roles: ops, driver, admin. API keys for webhooks. The tracking page gets signed per-order links.
2. Personal data: India's DPDP Act 2023 applies (names, phones, addresses). Consent text in the first message,
   retention limits, export/delete per customer, and no phone numbers in logs.
3. Ops basics: backups for Postgres, structured logs, health checks, error alerts, rate limits on public endpoints.
4. Background work off the request thread: triage and re-planning on a Redis queue (Redis is already in Compose).
5. Load: 500 orders / 20 vans per depot planned within the solver time limit.

**Done when** a deployment on D5 runs for a week of twin traffic without manual fixes.

### Step 5 · P7 Real providers and scale-out (L)

1. Messaging provider (D6) behind the `Messenger` port, with template approval and delivery receipts.
2. Weather and traffic signals (`signals/`): IMD / OpenWeather rain alerts as incident reports into the same triage.
3. Multiple depots, and moving orders between them.
4. EV vans: range and charging stops in the solver.

**Done when** swapping a provider needs only config, and EV plans never exceed range.

### Step 6 · 1.0

1. Pilot with one real fleet (even a small one) for a month: real replies replace the twin's `ASSUMPTIONS`,
   and the prevention number is re-measured on real deliveries.
2. Turn on `RISK_HOLDOUT` per D1 if the pilot has the volume.
3. Spec frozen as "1.0 as built"; README claims only what the pilot measured.

## Things I'd cut if time is short

- React console rewrite (keep HTML; only the driver app needs a framework).
- EV and multi-depot (P7.3–4): nothing in Chennai last-mile demands them first.
- POD photo check: depends on vision quality we haven't validated.
