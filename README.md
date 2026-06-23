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

## Environments

The same single FastAPI app runs in two environments. The app code is identical;
only the runtime and how the three client surfaces are reached differ. The web
page, GPS module, and Telegram are all available in **both**.

| Surface | Development (local) | Production (Hostinger) |
|---|---|---|
| **Run** | bare-metal `uvicorn ... --reload` on `:8000` | `docker compose up -d --build` (Traefik + app) |
| **Web** | `http://localhost:8000/dashboard` | `https://${DOMAIN_NAME}/dashboard` |
| **GPS** (Overland) | `https://<ngrok>.ngrok-free.app/gps` | `https://${DOMAIN_NAME}/gps` |
| **Telegram** | `PUBLIC_BASE_URL=https://<ngrok>.ngrok-free.app` | `PUBLIC_BASE_URL=https://${DOMAIN_NAME}` |

## Development (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill keys; runs keyless for local testing
uvicorn app.main:app --reload --port 8000
```

The web dashboard is at `http://localhost:8000/dashboard`. Telegram and the
phone's Overland GPS pings need a **public HTTPS** URL, which a local machine
doesn't have — expose it with ngrok in a second terminal:

```bash
ngrok http --domain=<your-ngrok-domain> 8000
# then set in .env:  PUBLIC_BASE_URL=https://<your-ngrok-domain>
# and restart uvicorn so it re-registers the Telegram webhook
```

Point Overland at `https://<your-ngrok-domain>/gps`.

## Production (Hostinger VPS)

Docker Compose runs **Traefik + app** in one stack. Traefik owns ports 80/443,
auto-provisions a Let's Encrypt cert for `${DOMAIN_NAME}`, and routes
`Host(${DOMAIN_NAME})` to the app over the internal Docker network.

```bash
cp .env.example .env          # set DOMAIN_NAME, SSL_EMAIL, PUBLIC_BASE_URL (literal https://<DOMAIN_NAME>) + keys
docker compose up -d --build  # Traefik gets its cert; app boots; Telegram webhook registers on startup
docker compose logs -f traefik   # watch ACME/cert issuance
```

- On boot, the app registers its Telegram webhook at `PUBLIC_BASE_URL/tg`
  (requires `TELEGRAM_BOT_TOKEN` + `PUBLIC_BASE_URL`).
- Point Overland at `https://${DOMAIN_NAME}/gps`.
- The app's own `127.0.0.1:8000` publish is loopback-only — for `curl` debugging
  on the VPS; Traefik is the public entrypoint, not that port.
- The SQLite DB lives in the `data` volume (`/data/kairos.db`) and survives
  recreations. Back up with:
  `docker run --rm -v activity-planner_data:/d alpine cat /d/kairos.db > kairos.db.bak`

## Notes / next

- `gather_conditions` uses a crude 07–20 daylight window — swap in a real
  sunrise/sunset calc (e.g. `astral`) keyed to Prague.
- Loop C (`jobs.enrichment`) is a deliberate stub: wire source fetch + LLM
  extraction, then a Telegram approval before inserting rows.
- Multi-hop causal queries (CHAINFORGE-style) are out of scope for the `rules`
  table; that's the line where a graph DB would earn its place back.
