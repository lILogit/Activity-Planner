# KAIROS — Conditional Leisure Time Management System

One FastAPI process. SQLite for everything (the "graph" is the `rules` table).
Direct Anthropic calls only where they earn it. APScheduler for the loops.
Telegram for plans and feedback. The only thing kept from the old design is the
**GPS mobile module** (Overland), which posts to `POST /gps`.

## Architecture (one service)

```
Overland (phone) ──POST /gps──▶ FastAPI ──┬─ enrich (OpenWeather + LocationIQ)
                                          ├─ insert ping (SQLite)
                                          └─ staypoint segment ─▶ classify (Haiku) ─▶ session
                                                                                       │
Telegram  ◀──plan / feedback request──  FastAPI  ◀── APScheduler (06:30 / Sun 18:00 / hourly)
   │                                       ▲
   └──POST /tg (buttons, ⭐ replies)───────┘  ── update Beta posterior
```

## Loops

- **A — Plan** (cron 06:30 daily, Sun 18:00 weekly): gather conditions → score `U(a|c,t)` → route NOW/NEXT/PARK/ARCHIVE → Telegram plan + Approve/Reroll/Skip.
- **B — Observe** (event, `/gps`): enrich → staypoint detect → classify vs venues + today's plan → session → request ⭐ → posterior update.
- **C — Enrich** (cron Mon 07:00): pull sources → propose new activities/venues for approval. *(stub in `jobs.enrichment`)*

## Files

```
app/
  main.py       FastAPI app, /gps + /tg endpoints, GPS→session pipeline, lifespan
  config.py     env-driven settings
  db.py         SQLite connection + init
  schema.sql    DDL  (pings, activities, venues, rules, sessions, feedback, goals, plans)
  seed.sql      starter inventory + causal rules + home anchor + golf goal
  geo.py        Haversine + staypoint segmentation
  scoring.py    Beta posterior, fit, UCB/Thompson, utility, routing
  enrich.py     OpenWeather + LocationIQ (graceful no-key fallback)
  llm.py        Anthropic: staypoint classify + plan rationale (heuristic fallback)
  telegram.py   send plan / feedback, parse webhook, log ⭐ → posterior
  planner.py    conditions → scored, routed, persisted plan
  jobs.py       APScheduler wiring for loops A & C + Kairos-window scan
```

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in keys; works with none for local testing
uvicorn app.main:app --reload --port 8000
```

Point Overland at `http://<host>:8000/gps`. Set `PUBLIC_BASE_URL` so the
Telegram webhook auto-registers on startup, or register `/tg` manually.

## Deploy (Hostinger VPS)

`uvicorn app.main:app --host 0.0.0.0 --port 8000` behind a TLS reverse proxy,
as a systemd unit or single Docker container. Back up with `cp kairos.db`.

## Notes / next

- `gather_conditions` uses a crude 07–20 daylight window — swap in a real
  sunrise/sunset calc (e.g. `astral`) keyed to Prague.
- Loop C (`jobs.enrichment`) is a deliberate stub: wire source fetch + LLM
  extraction, then a Telegram approval before inserting rows.
- Multi-hop causal queries (CHAINFORGE-style) are out of scope for the `rules`
  table; that's the line where a graph DB would earn its place back.
