# LookAtDisPatch: game plan (portfolio scope)

Draft for review · 2026-10-20 · companion to [v2-spec.md](v2-spec.md).

**Scope.** This is a portfolio project. It's finished when four things work well and are easy to show:

1. **Routing**: OR-Tools plans on real roads, re-plans around problems.
2. **Agentic interface**: the ops chat and the decision layer (AI triage under confidence gates, a person
   when unsure, a ledger of every decision).
3. **Mock notifications**: simulated customer messages and replies, end to end.
4. **Simulations**: the day replay and learner, and the failed-delivery comparison.

Everything else in the spec (phases P4, P6, P7, production concerns) is out of scope; see the end.

## Where we stand

All four already work and are tested (140 offline tests). What's left is making them hold up in a live
demo and making them easy to see.

| Area | Works | Weak spots |
|---|---|---|
| Routing | OSRM matrix + road geometry, 3 strategies + critic, live re-plans, "no earlier than promised", detours around zones (old route dashed), 50 × 5 in 4.5 s | Done (step 1) |
| Agentic interface | Chat with 12 tools, incident and reply triage, gates, human queue, ledger, `python -m dispatch.eval` | Live triage verified once; the full eval run is waiting on quota (see step 3) |
| Mock notifications | ETA updates, confirmations, cancel / noted / rescheduled / reattempt / returning messages; simulated replies; threads per customer | Done (step 2) |
| Simulations | Day replay + learner (delay error 26% → 14% → 6%), prevention twin (28% fewer failures), ▶ Play a day | Done (step 2) |

## Decisions

| # | Decision | Recommendation |
|---|---|---|
| D1 | `RISK_HOLDOUT` | **Leave at 0 and don't revisit.** It only matters with real deliveries. The code and the twin finding stay as a talking point |
| D2 | Gemini | **Free tier, plus a demo that works without it.** Rules already settle button replies, times, risk and failed attempts. Add recorded AI answers for the scripted demo (step 2) so a quota error never breaks a showing. Billing only if you want to run the accuracy check (step 3) |
| D3 | Hosting | **Optional.** If you want a live link: one small VM or Render/Fly with SQLite + straight-line routing and no key (read-only demo mode). Otherwise a screen recording |

## Plan

Sizes: S ≈ a session, M ≈ 2–3.

### Step 1 · Visible detours (done)

Built with via points on a ring around each zone rather than OSRM alternatives (alternatives rarely
avoid a given area; a via point does, and one extra matrix call prices all of them). See spec §8.
Benchmark: 50 × 5 in 4.5 s, 100 × 8 in 4.4 s.

### Step 2 · "Play a day" (done)

Built as ordered steps paced in real time rather than a simulated clock (the system's times stay real,
so ETAs and messages read naturally). See spec §9. Playing it exposed two solver issues, both fixed:
an evening reschedule made one van take every stop (balance now uses work, not clock time), and the
4-hour waiting cap made late windows unreachable.

### Step 3 · Agent polish (done; one number outstanding)

- Chat tools added: `delivery_problems`, `decide_failed_delivery`, `play_a_day` (demo mode only).
- 30 labelled reports (`dispatch/evals/incident_reports.jsonl`: English, Tamil, Hindi, and both in Latin
  script) and `python -m dispatch.eval`: kind and outcome accuracy, automation and wrong-auto rates, per
  language. Answers are cached, so a run cut short by quota resumes the next day.
- Live triage worked once (a Velachery flood, classified and acted on). The full run then hit the daily
  free quota (429) and an overload (503). A run costs about 90 calls (3 per report with logprobs), so it
  takes ~5 days of free quota, resumed each day, or a few minutes with billing (D2).
- Found and fixed on the way: a 429 or 503 on the first call made `auto` mode give up on logprobs for
  good, switching to sampling (5+ calls per report).

### Step 4 · Portfolio packaging (done; hosted demo optional, not done)

- README: a GIF of a played day and three stills (`docs/img/`, recorded headlessly from the real console
  on OSRM with no key), an architecture diagram (Mermaid), and a "Design decisions" section.
- Removed the empty `dispatch/eta/` and `dispatch/signals/` packages and the `/twin/replay` 501 stub; added
  `GET /events` (the console uses it with `?nostream=1`, which is how the frames were captured).
- The console shows "AI recorded" when recorded answers stand in for a missing key.
- To re-record the images: run the API with `DEMO_MODE=1`, serve `web/`, POST `/demo/play?pace_s=7`, and
  screenshot `console.html?nostream=1` every few seconds (Edge or Chrome `--headless=new --screenshot`).
- Optional hosted demo (D3): not done.

**Done when** someone can clone, run `python run.py`, press ▶ Play a day, and watch routing, AI decisions,
messages and learning happen without a key, in under five minutes.

## Not doing (stays in the spec as "future")

- P4 addresses: geocoding, DIGIPIN, drop-a-pin. Orders keep their coordinates.
- P5 beyond what exists: quantile ETA model, promise policy, threshold tuning.
- P6 driver app, tracking page, photo checks.
- P7 real WhatsApp/SMS/weather providers, multiple depots, EV vans.
- Production: auth, data-protection compliance, backups, job queue, scale targets.
- The trained model's holdout (D1), Jev beyond what's there, a React rewrite.
