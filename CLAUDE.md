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
| `Dockerfile` | Image for the single uvicorn process (non-root, `/data` volume) | image / runtime deps |
| `Dockerfile.caddy` | `caddy:2` + the Caddyfile baked in (NOT bind-mounted — Hostinger doesn't sync sibling files to its runtime dir) | Caddy image |
| `docker-compose.yml` | app + Caddy (auto-TLS), volumes, healthcheck | Hostinger deploy |
| `Caddyfile` | Caddy reverse proxy; `{$DOMAIN}` → `app:8000`, auto Let's Encrypt | TLS / domain |

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/gps` | Overland GPS ingest |
| `POST` | `/tg` | Telegram webhook |
| `GET` | `/dashboard` | Web dashboard (HTML) |
| `GET` | `/api/state` | Dashboard data snapshot (JSON) |
| `POST` | `/api/enrich` | Free-text → extract events → insert venues/goals |
| `POST` | `/api/feedback` | Log star rating for a session, update posterior |
| `GET` | `/admin` | Lightweight counts + activity list |
| `GET` | `/health` | `{"status":"ok"}` |

### Deployment topology (Docker, Hostinger)

Two containers, but still **one app process + one datastore** — Caddy is only a TLS
terminator, not application logic, so it doesn't violate Golden Rule #1:

```
Internet ──80/443──▶ Caddy (auto Let's Encrypt for {$DOMAIN})
                        │  reverse_proxy, sets X-Forwarded-*
                        ▼ (internal docker net, port 8000)
                     app (uvicorn + APScheduler, single process)
                        │  sqlite3.connect("/data/kairos.db")
                        ▼
                     data volume  ──(kairos.db persists across recreations)
```

On boot, `lifespan` calls Telegram `setWebhook` with `PUBLIC_BASE_URL + "/tg"` — so
`PUBLIC_BASE_URL` must be the public `https://<DOMAIN>` Caddy serves. Don't run a
second app replica: SQLite + the in-process scheduler assume a single writer.

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
cp .env.example .env          # runs keyless; fill keys to enable features

# Dev server (auto-reload on file changes)
.venv/bin/uvicorn app.main:app --reload --port 8000

# Expose locally via ngrok (required for Telegram webhook)
ngrok http --domain=<your-domain> 8000

# Smoke test (no keys, no network)
.venv/bin/python3 - <<'EOF'
import os, tempfile, time
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
from app.db import init_db
from app.scoring import Conditions, fit, update_posterior, rule_effect
from app.geo import segment_staypoints
init_db(seed=True)
from app.db import get_conn
with get_conn() as c:
    golf = dict(c.execute("SELECT * FROM activities WHERE name='golf'").fetchone())
    rules = [dict(r) for r in c.execute("SELECT * FROM rules").fetchall()]
cond_clear = Conditions(season="spring", daytime=True, temp=18, weather_main="Clear")
cond_rain  = Conditions(season="spring", daytime=True, temp=18, weather_main="Rain")
assert fit(golf, cond_clear) > 0.8
blocked, _ = rule_effect(golf["id"], rules, cond_rain)
assert blocked
a, b = 1.0, 1.0
for s in (5, 5, 4): a, b = update_posterior(a, b, s)
assert a / (a + b) > 0.7
now = int(time.time())
pings = [{"id": i, "ts": now + i*60, "lat": 49.96, "lon": 14.38} for i in range(15)]
pings += [{"id": 99, "ts": now + 15*60, "lat": 49.97, "lon": 14.4}]
res = segment_staypoints(pings, 150, 720)
assert len(res.closed) == 1
print("All smoke tests passed.")
EOF

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

# Deploy (Hostinger VPS) — Docker Compose: app + Caddy (auto-TLS)
cp .env.example .env          # set DOMAIN, PUBLIC_BASE_URL, and your keys
docker compose up -d --build  # Caddy gets a Let's Encrypt cert; app boots
docker compose logs -f app    # follow startup (Telegram webhook registers here)
docker compose ps             # both services + healthcheck status

# The SQLite DB lives in the `data` volume (/data/kairos.db), set via
# compose `environment` (overrides any DB_PATH in .env). Backup the volume:
docker run --rm -v kairos_data:/d alpine cat /d/kairos.db > kairos.db.bak

# Bare-metal alternative (behind your own TLS reverse proxy):
# uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Env vars (`.env`)

`ANTHROPIC_API_KEY` (empty ⇒ heuristic mode) · `MODEL_FAST`=`claude-haiku-4-5-20251001`
· `MODEL_SMART`=`claude-sonnet-4-6` · `OPENWEATHER_API_KEY` · `LOCATIONIQ_API_KEY` ·
`TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID` · `PUBLIC_BASE_URL` (https URL the Telegram
webhook points at) · `DOMAIN` (Caddy auto-TLS host; consumed by `Caddyfile` via
`{$DOMAIN}`) · `DB_PATH` (compose sets `/data/kairos.db`; bare-metal defaults to
`kairos.db`) · `STAYPOINT_RADIUS_M` (150) · `STAYPOINT_MIN_DWELL_S` (720) · scoring
weights `W_PREF/W_FIT/W_PROG/W_EXPL` · `TZ`=`Europe/Prague`.

After changing `.env`, restart the server — pydantic-settings reads env only at startup.
For Docker: `docker compose up -d` re-creates the container and re-reads `.env`.

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
