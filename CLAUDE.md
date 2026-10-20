# Project notes for Claude

The product is **LookAtDisPatch** (shown in the console, API docs and README); the Python package stays `dispatch`.

## What's here
- `dispatch/` is **v2**, the only version (v1 `dispatch_agent/` and `logistics_agent/` were removed).
  The spec is `specs/v2-spec.md`. Update the spec in the same change whenever code needs to differ from it.
- `specs/1230-spec.md` is the original hackathon spec, for history only.
- `README.md` is the user-facing overview and run guide (there is no separate RUN.md). Keep its
  "Try it" table in step with the console.

## Running v2
- Everything: `python run.py` (Docker services if available, demo data, API on :8080, console `web/console.html`
  on :8001, Gemini key dialog). `--fresh` resets the demo, `--no-docker` uses SQLite.
- Infra only: `docker compose -f infra/docker-compose.yml up -d` (add `--profile osrm` for routing; see `infra/README.md`).
- API: `python -m dispatch.api` on http://127.0.0.1:8080 (docs at `/docs`). Set
  `DATABASE_URL=postgresql+psycopg://dispatch:dispatch@127.0.0.1:5432/dispatch` for Postgres; unset = SQLite.
- Store tests on Postgres too: `TEST_DATABASE_URL=postgresql+psycopg://dispatch:dispatch@127.0.0.1:5432/dispatch_test pytest tests/test_store.py`.

## Working rules
- Never commit or push. The user handles git.
- Run `pytest -q` before saying something works; tests must not need network or AI keys.
- New port implementations (`dispatch/core/ports.py`) must pass their check in `tests/contracts/checks.py`.
- Contracts in `dispatch/core/` use seconds and meters, UTC-aware datetimes, and `state` (never
  `status`) for an object's own lifecycle, because `status` is the tool-result field.

## Gotchas learned the hard way
- **No `agent.py` in the repo root.** ADK then switches to single-agent mode and hides every agent folder.
- **No `:` in filenames.** Windows can't check them out.
- **Keys:** read from the repo-root `.env`; real environment variables win over it.
- **Model:** `GEMINI_MODEL`, default `gemini-3.6-flash`. `gemini-2.5-flash`
  returns 404 for new AI Studio keys. `gemini-flash-latest` shares `gemini-3.8-flash`'s quota.
- **Gemini free tier:** about 5 requests/min **and** about 20 requests/day per model per project.
  A new key in the same project shares the quota. Triage costs 3 calls per report with logprobs (5+ with
  sampling), so `python -m dispatch.eval` (30 reports) needs several days of free quota; it caches answers
  in `.eval_cache.json` and resumes. Don't burn quota on ad-hoc live checks before a demo.
- **Windows file locks:** writing a file another process is reading can raise `PermissionError`
  on `os.replace`. Retry it.
- **Windows time zones** need the `tzdata` package (in requirements).
- **Docker is needed for v2 infra** (Postgres/PostGIS, Redis, OSRM: see `infra/README.md`).
  Without it, v2 falls back to SQLite and straight-line routing.
- **Headless screenshots of the console:** add `?nostream=1` (an open SSE stream keeps headless Chrome/Edge
  from ever finishing); the activity feed then loads once from `GET /events`.
- **Use `127.0.0.1`, not `localhost`, for local services.** On Windows `localhost` tries IPv6 `::1`
  first while Docker listens on IPv4 only: every connection then stalls ~8 s.
- **Docker is installed per-user** (`%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin`). Shells started
  before the install don't have it on PATH: add that folder or reload PATH from the registry.
