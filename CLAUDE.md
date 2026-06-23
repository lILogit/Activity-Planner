# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# KAIROS — Conditional Leisure Time Management System

**One FastAPI process, one SQLite file.**
GPS in → enrich → detect staypoints → classify → score the day → Telegram plan →
human ⭐ → learn. The only external moving part is the **Overland GPS app** on the
phone, which posts to `POST /gps`.

---

## Golden rules (do not break)

1. **One process, one datastore.** No N8N, no Neo4j, no message broker, no second
   service. If a task seems to need another runtime, it almost certainly doesn't at
   this scale — push back before adding one.
2. **The LLM lives only in `app/llm.py`.** Every other module is deterministic and
   unit-testable. All LLM functions MUST keep their heuristic fallback so the whole
   service runs with **zero API keys**.
3. **External calls degrade to `{}`.** `app/enrich.py` (weather, geocode) returns an
   empty dict when its key is missing. Never let a missing key raise.
4. **The "graph" is the `rules` table.** Causal edges are rows
   `(cond_type, cond_value, activity_id, kind, weight)`, `kind ∈ {activate, block,
   modulate}`. A SQL filter over active conditions replaces graph traversal. Do not
   reintroduce a graph DB for ≤2-hop logic.
5. **Anchor suppression is sacred.** Dwells at an `is_anchor` venue (home) are never
   activities. They get sentinel `session_id = 0` so they aren't reprocessed.
6. **Time is epoch seconds everywhere.** Parse inbound timestamps once, in
   `main._parse_ts`.

---

## Architecture

```
Overland (phone) ──POST /gps──▶ FastAPI ──┬─ enrich (OpenWeather + LocationIQ)
                                          ├─ insert ping (SQLite)
                                          └─ staypoint segment ─▶ classify (Haiku) ─▶ session
                                                                                       │
Telegram  ◀──plan / feedback──  FastAPI  ◀── APScheduler (06:30 / Sun 18:00 / hourly)
   │                              ▲
   └──POST /tg (buttons, ⭐)──────┘  ── update Beta posterior

Browser ──GET /dashboard──▶ FastAPI ──GET /api/state──▶ live JSON snapshot
                  └──POST /api/enrich──▶ LLM extract + geocode + DB insert
                  └──POST /api/feedback──▶ stars → Beta posterior update
```

### The three loops
- **A — Plan** (`jobs.morning_plan` 06:30, `jobs.weekly_plan` Sun 18:00):
  `planner.build_and_send_plan` → gather conditions → score `U(a|c,t)` → route
  NOW/NEXT/PARK/ARCHIVE → Telegram plan with Approve / Reroll / Skip.
- **B — Observe** (event, `POST /gps`): enrich → `geo.segment_staypoints` over the
  unassigned ping tail → `llm.classify_staypoint` against nearby venues + today's
  approved plan → create session → `telegram.request_feedback` → ⭐ updates posterior.
- **C — Enrich** (`POST /api/enrich`, `jobs.enrichment` Mon 07:00): free text → `llm.extract_events`
  → geocode → upsert venue/activity/goal. Scheduler job is still a stub for automated sources.

---

## Module map

| File | Responsibility | Touch when |
|---|---|---|
| `app/main.py` | FastAPI app, all endpoints, GPS→session pipeline, lifespan | endpoints, ingest pipeline |
| `app/dashboard.html` | Single-page dashboard (vanilla JS, fetches `/api/state`) | UI changes |
| `app/tables.html` | Generic CRUD editor UI for `/tables` (drives `/api/{table}` endpoints) | inventory-editing UI |
| `app/config.py` | env-driven `Settings` (keys, staypoint params, scoring weights, tz) | new config knob |
| `app/db.py` | SQLite connect + `init_db` (runs `schema.sql` then `seed.sql`) | connection concerns |
| `app/schema.sql` | DDL — all tables. `CREATE IF NOT EXISTS` (idempotent) | schema change |
| `app/seed.sql` | starter inventory, rules, home anchor, golf goal. `INSERT OR IGNORE` | starter data |
| `app/geo.py` | `haversine_m`, `segment_staypoints` (centroid radius + min dwell) | matching geometry |
| `app/scoring.py` | `Conditions`, `fit`, `rule_effect`, `utility`, `select`, `update_posterior`, UCB/Thompson, routing | the math |
| `app/enrich.py` | OpenWeather + LocationIQ (weather cache, keyless fallback) | external data |
| `app/llm.py` | `classify_staypoint`, `plan_rationale`, `extract_events`; heuristic fallbacks | LLM behavior |
| `app/telegram.py` | send plan/feedback, parse webhook, log ⭐ → posterior | bot I/O |
| `app/planner.py` | `gather_conditions`, `_goal_gap`, `build_plan`, `build_and_send_plan` | planning logic |
| `app/jobs.py` | APScheduler wiring for loops A & C + `kairos_window` scan | schedules |
| `Dockerfile` | Image for the single uvicorn process (non-root, `/data` volume, `--proxy-headers`) | image / runtime deps |
| `docker-compose.yml` | `traefik` service (TLS/routing) + `app` service, `data`/`traefik_data` volumes, `/health` healthcheck | Hostinger deploy |

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/gps` | Overland GPS ingest |
| `POST` | `/tg` | Telegram webhook |
| `GET` | `/dashboard` | Web dashboard (HTML) |
| `GET` | `/tables` | Generic CRUD editor UI for inventory tables (HTML) |
| `GET` | `/api/state` | Dashboard data snapshot (JSON) |
| `POST` | `/api/enrich` | Free-text → extract events → insert venues/goals |
| `POST` | `/api/feedback` | Log star rating for a session, update posterior |
| `PATCH` | `/api/activity/{id}` | Update an activity's `cluster`/`active` flag |
| `GET/POST` | `/api/{table}` | Generic CRUD list/create over `activities`, `venues`, `rules`, `goals` (`_CRUD_TABLES` in `main.py`) |
| `GET/PUT/DELETE` | `/api/{table}/{row_id}` | Generic CRUD get/update/delete for the same tables; deletes cascade/unlink FKs (see comments in `main.py`) |
| `POST` | `/api/import/create-venues-from-sessions` | Backfill venues at session centroids so future GPS pings auto-match (closes the CSV-import → live-GPS gap) |
| `POST` | `/api/import/gps-csv` | One-off idempotent import of `GPS records.csv` from the project root into `pings`/`sessions`/`feedback` |
| `GET` | `/admin` | Lightweight counts + activity list |
| `GET` | `/health` | `{"status":"ok"}` |

Pipeline tables (`pings`, `sessions`, `feedback`, `plans`) are intentionally excluded
from the generic CRUD surface — they're written by the GPS/scoring pipeline, not
hand-edited.

### Deployment topology (Docker, Hostinger)

Two containers in one compose stack: a `traefik` service terminates TLS (Let's
Encrypt via the TLS-ALPN challenge) and routes by `Host()` to the `app` container
over the internal Docker network, discovered via `traefik.*` labels:

```
Internet ──443──▶ traefik container  (Let's Encrypt cert, Host(`${DOMAIN_NAME}`) routing)
                      │  routes to app:8000 over the docker network
                      ▼
                   app container  (uvicorn + APScheduler, single process)
                      │  also published to 127.0.0.1:8000 — for VPS-local debugging only
                      ▼
                   data volume  ──(kairos.db persists across recreations)
```

`traefik` owns host ports 80/443; the app's own port publish
(`127.0.0.1:8000:8000`) is loopback-only and exists purely so you can `curl
localhost:8000` from the VPS directly — it is never the public entrypoint.
uvicorn runs with `--proxy-headers`, so it trusts `X-Forwarded-*` from Traefik.
On boot, `lifespan` calls Telegram `setWebhook` with `PUBLIC_BASE_URL + "/tg"`,
so set `PUBLIC_BASE_URL` to a literal `https://${DOMAIN_NAME}` value in `.env`
(env_file values aren't variable-expanded, so it can't reference `${DOMAIN_NAME}`
directly — see `.env.example`). Don't run a second app replica: SQLite + the
in-process scheduler assume a single writer.

A prior iteration ran a host-level reverse proxy (nginx) instead of a
containerized one — this was superseded in favor of mirroring an already-working
Traefik pattern from another deployment on the same Hostinger account. If you
add another container-based service later, give it its own `Host()` rule and
join the same `traefik` service rather than standing up a second proxy.

### Environments: Development vs Production

The same single FastAPI app runs in two environments — the app code is identical;
only the runtime and how the three client surfaces are reached differ. The web
page, GPS module, and Telegram are all available in **both**.

| Surface | Development (local bare-metal) | Production (Hostinger, Traefik) |
|---|---|---|
| **Run** | `KAIROS_ENV_FILE=.env.test uvicorn … --reload` | `docker compose up -d --build` (traefik + app) |
| **Env file** | `.env.test` (gitignored) | `.env.prod` (gitignored; compose `env_file`) |
| **Web** (`/dashboard`, `/tables`) | `http://localhost:8000/dashboard` | `https://${DOMAIN_NAME}/dashboard` |
| **GPS** (Overland `POST /gps`) | `https://<ngrok>.ngrok-free.app/gps` | `https://${DOMAIN_NAME}/gps` |
| **Telegram** webhook (`/tg`) | `PUBLIC_BASE_URL=https://<ngrok>.ngrok-free.app` | `PUBLIC_BASE_URL=https://${DOMAIN_NAME}` |

**Development** has no public TLS of its own, so **ngrok** provides the public
HTTPS that both the Telegram webhook and the phone's Overland pings require: run
`ngrok http --domain=<your-ngrok-domain> 8000` in a second terminal, set
`PUBLIC_BASE_URL` to that ngrok URL, and restart uvicorn so it re-registers the
webhook. The web dashboard stays on plain `http://localhost:8000`.

**Production** is the Traefik stack above: Traefik owns 80/443, obtains the Let's
Encrypt cert for `${DOMAIN_NAME}`, and routes `Host(${DOMAIN_NAME})` to the app.
`DOMAIN_NAME` / `SSL_EMAIL` are compose-only vars — Development doesn't use them.

---

## Data model (SQLite)

`pings` (raw GPS log; `session_id` set once assigned) · `activities`
(`alpha`/`beta` = Beta posterior over ⭐; `active=0` ⇒ ARCHIVED) · `venues`
(`is_anchor` excludes from matching) · `rules` (the flattened causal edges) ·
`sessions` (one classified staypoint) · `feedback` · `goals` · `plans`
(`payload` is JSON: conditions + ranked picks + reasoning).

Full DDL in `app/schema.sql`. Both `schema.sql` and `seed.sql` are idempotent —
when you add a column, guard it so re-running stays safe (`CREATE IF NOT EXISTS`
or a guarded `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`).

`pings.session_id` intentionally has **no FK** — the sentinel value `0` marks
anchor-suppressed home dwells; a REFERENCES constraint would reject that write.

---

## Scoring (the heart, `app/scoring.py`)

```
U(a|c,t) = ( w_pref·Pref + w_fit·Fit + w_prog·Prog + w_expl·Explore ) · rule_factor
```
- `Pref` = `beta_mean(alpha, beta)`; `Fit` ∈ [0,1] from season/daylight/temp gates.
- `Prog` = fraction of a goal's cadence still unmet (`planner._goal_gap`).
- `Explore` = UCB `sqrt(2·ln N / n_a)`; `select()` reserves `explore_ratio` of slots
  for a Thompson/under-explored swap.
- A matching **block** rule ⇒ `U = 0` and route `PARK`. Weights live in `config.py`.
- Learning: `update_posterior(alpha, beta, stars)` maps stars 1–5 → success in [0,1].
  This is the ONLY place preferences change (called from `telegram._handle_text` and `main.add_feedback`).

Routing: `NOW` (fit ≥ 0.6, unblocked) · `NEXT` (fit 0.2–0.6) · `PARK`
(blocked or fit < 0.2) · `ARCHIVE` (≥5 samples and pref < 0.25).

---

## Conventions

- Async for all network I/O (`httpx.AsyncClient`); SQLite ops are short and sync
  (fine at personal scale). Don't block the event loop with long sync work.
- Telegram callbacks use `"action:plan_id"` data; the awaiting-feedback session is
  the most recent with `feedback_requested = 1` and no `feedback` row.
- Overland payload shape: `locations[].geometry.coordinates = [lon, lat]`,
  `locations[].properties.{timestamp, altitude, wifi}`. Parse in `/gps`.
- New scheduled work → add a job in `jobs.build_scheduler`, not a new process.
- Keep `/gps` fast: return `{"result":"ok"}` immediately after writing; classification
  (Haiku + heuristic fallback) is the only async work done per ping.
- `enrich.fetch_weather` caches responses 10 min per ~5 km grid cell (`_weather_cache`).
  Do not remove this — Overland can ping every 10 s.
- `llm.extract_events` returns `[]` when no API key is set; callers must handle that.
- Venue inserts from `/api/enrich` are skipped if geocoding fails — a `(0,0)` venue
  would never match GPS and wastes a row.

---

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.test     # dev env (gitignored); fill keys to enable features
cp .env.example .env.prod     # prod env (gitignored); set DOMAIN_NAME/SSL_EMAIL/keys

# Dev server (auto-reload). KAIROS_ENV_FILE selects .env.test (config.py reads it):
KAIROS_ENV_FILE=.env.test .venv/bin/uvicorn app.main:app --reload --port 8000

# Expose locally via ngrok (required for Telegram webhook)
ngrok http --domain=<your-domain> 8000

# Smoke test (no keys, no network) — fit/block/posterior/staypoint-segmentation asserts
.venv/bin/python3 tests/smoke.py

# E2E probe (no keys; requires the dev server running on :8000) — walks all three
# loops over real HTTP: anchor suppression, GPS→session→feedback, plan approval
.venv/bin/python3 tests/e2e_probe.py

# Trigger a plan immediately (bypasses 06:30 scheduler)
.venv/bin/python3 -c "import asyncio; from app.planner import build_and_send_plan; asyncio.run(build_and_send_plan('daily'))"

# Test GPS ingest
curl -s -X POST http://localhost:8000/gps \
  -H "Content-Type: application/json" \
  -d '{"locations":[{"geometry":{"coordinates":[14.42,50.08]},"properties":{"timestamp":"2024-06-01T10:00:00Z","altitude":280}}]}'

# Enrich via API
curl -s -X POST http://localhost:8000/api/enrich \
  -H "Content-Type: application/json" \
  -d '{"text": "Golf tournament in Hluboká nad Vltavou on 19.6.2026"}'

# Deploy (Hostinger VPS) — Traefik (TLS + routing) + app, one compose stack
# Edit .env.prod: set DOMAIN_NAME, SSL_EMAIL, PUBLIC_BASE_URL (literal https://<DOMAIN_NAME>) + keys
docker compose up -d --build  # compose injects .env.prod; traefik gets its cert; app boots; Telegram webhook registers on startup
docker compose logs -f traefik   # watch ACME/cert issuance
docker compose logs -f app
docker compose ps

# Traefik owns ports 80/443 and routes Host(`${DOMAIN_NAME}`) to the app over
# the docker network. The app's own 127.0.0.1:8000 publish is loopback-only,
# for local debugging on the VPS — never the public entrypoint.

# The SQLite DB lives in the `data` volume (/data/kairos.db), set via
# compose `environment` (overrides any DB_PATH in .env). Backup the volume:
docker run --rm -v activity-planner_data:/d alpine cat /d/kairos.db > kairos.db.bak

# Bare-metal alternative:
# uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Env vars (`.env`)

`ANTHROPIC_API_KEY` (empty ⇒ heuristic mode) · `MODEL_FAST`=`claude-haiku-4-5-20251001`
· `MODEL_SMART`=`claude-sonnet-4-6` · `OPENWEATHER_API_KEY` · `LOCATIONIQ_API_KEY` ·
`TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID` · `PUBLIC_BASE_URL` (public https URL,
literally `https://${DOMAIN_NAME}` — not expanded by env_file, so spell it out;
the app registers its webhook at `PUBLIC_BASE_URL/tg`) · `DOMAIN_NAME` (compose-only,
read by `docker-compose.yml`'s Traefik `Host()` rule and cert request) ·
`SSL_EMAIL` (compose-only, Let's Encrypt expiry/revocation contact) ·
`DB_PATH` (compose sets `/data/kairos.db`; bare-metal defaults to `kairos.db`) ·
`STAYPOINT_RADIUS_M` (150) · `STAYPOINT_MIN_DWELL_S` (720) · scoring weights
`W_PREF/W_FIT/W_PROG/W_EXPL` · `TZ`=`Europe/Prague`.

After changing `.env`, restart the server — pydantic-settings reads env only at startup.
For Docker: `docker compose up -d` re-creates the container and re-reads `.env`.

**Env files are per environment:** `.env.test` (dev, loaded via
`KAIROS_ENV_FILE=.env.test`) and `.env.prod` (prod, injected by compose `env_file`).
`.env.example` is the committed template; the two real files are **gitignored**
(they hold secrets).

---

## Open work (deliberate stubs — implement in place)

1. **Daylight calc** — `planner.gather_conditions` uses a crude 07–20 window. Swap in
   `astral` keyed to Prague for real sunrise/sunset.
2. **Loop C scheduler** — `jobs.enrichment` is a no-op. The manual `/api/enrich` endpoint
   works; wire automated source fetch (RSS, calendar) + Telegram approval to the scheduler.
3. **Weekly plan variety** — `build_plan(k=3)` exists; tune a cluster-diversity penalty
   so a weekly plan isn't all SPORT.

## Out of scope

Multi-hop causal chains (3+ hops) don't fit the `rules` table — that's where a real
graph store earns its place back. Don't bolt partial graph semantics onto SQLite;
raise it as a design decision instead.
