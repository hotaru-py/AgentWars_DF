# Local infrastructure (v2)

Needs [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
docker compose -f infra/docker-compose.yml up -d          # Postgres + PostGIS, Redis
python infra/prepare_osrm.py                              # once: road network for Chennai (add --full for the Southern Zone)
docker compose -f infra/docker-compose.yml --profile osrm up -d   # OSRM on http://127.0.0.1:5000
```

| Service | Port (localhost only) | Env var |
|---|---|---|
| Postgres 16 + PostGIS 3.4 | 5432 | `DATABASE_URL=postgresql+psycopg://dispatch:dispatch@127.0.0.1:5432/dispatch` |
| Redis 7 | 6379 | `REDIS_URL=redis://127.0.0.1:6379/0` |
| OSRM 5.25 (car, MLD) | 5000 | `OSRM_URL=http://127.0.0.1:5000` |

Without Docker, the app still runs for development and tests: the store falls back to SQLite
(`DATABASE_URL` unset) and routing falls back to straight-line distance (x1.3 at 25 km/h).

The credentials above are for local development only. `infra/osrm-data/` is git-ignored (several GB).
