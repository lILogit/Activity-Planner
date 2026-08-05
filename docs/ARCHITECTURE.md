# KAIROS — Framework Logic & Principles

> Conditional Leisure Time Management System.
> **GPS in → enrich → detect staypoints → classify → score the day → Telegram plan → human ⭐ → learn.**
> One FastAPI process, one SQLite file.

---

## The 6 Golden Rules (Inviolable)

These govern every design decision. Breaking one requires raising it as
an architecture question — do not silently violate them.

### 1. One Process, One Datastore
Single FastAPI process + single SQLite file. No N8N, no Neo4j, no message
broker, no second service. If a task seems to need another runtime, it almost
certainly doesn't at this scale — push back before adding one.

### 2. The LLM Lives Only in `app/llm.py`
Every other module is deterministic and unit-testable. All LLM functions
(`classify_staypoint`, `plan_rationale`, `extract_events`) MUST keep a heuristic
fallback so the whole service runs with **zero API keys**.

### 3. External Calls Degrade to `{}`
`app/enrich.py` (weather, geocode) returns an empty dict when its key is
missing. Never let a missing key raise.

### 4. The "Graph" Is the `rules` Table
Causal edges are rows `(cond_type, cond_value, activity_id, kind, weight)`,
`kind ∈ {activate, block, modulate}`. A SQL filter over active conditions
replaces graph traversal. Do **not** reintroduce a graph DB for ≤2-hop logic.
Multi-hop (3+) is out of scope — that's where a real graph store earns its place.

### 5. Anchor Suppression Is Sacred
Dwells at an `is_anchor` venue (home) are never activities. They get sentinel
`session_id = 0` so they aren't reprocessed.

### 6. Time Is Epoch Seconds Everywhere
Parse inbound timestamps once, in `main._parse_ts`. No ISO strings in the DB.

---

## The Three Loops

```
    ┌──────────────────────────────────────────────────────────┐
    │                     LOOP A — PLAN                        │
    │  (jobs.morning_plan 06:30 / jobs.weekly_plan Sun 18:00)  │
    │                                                          │
    │  gather_conditions ──▶ score U(a|c,t) ──▶ route ──▶ plan │
    │         │                    │              │       │    │
    │    weather, season      all activities   NOW/NEXT    │    │
    │    daylight, temp       + rules factor   /PARK/      ▼    │
    │                                        ARCHIVE   Telegram │
    │                                             │   Approve/  │
    │                                             ▼   Reroll/Skip
    │                                         plans table       │
    └──────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────┐
    │                   LOOP B — OBSERVE                        │
    │              (event-driven, POST /gps)                    │
    │                                                          │
    │  Overland ping ──▶ enrich ──▶ insert ping ──▶ segment    │
    │       │              │                         │         │
    │  /gps endpoint   weather cache          staypoint closes  │
    │  returns {ok}    geocode addr                 │           │
    │                                              ▼           │
    │                                    ┌─── anchor? ──▶ sid=0│
    │                                    │   (suppress)        │
    │                                    ▼                     │
    │                              classify_staypoint          │
    │                              (LLM + heuristic)           │
    │                                    │                     │
    │                              create session              │
    │                              auto-create venue           │
    │                                    │                     │
    │                              request_feedback ──▶ ⭐ ──▶ │
    │                                                   │      │
    │                                              update_posterior│
    │                                              (Beta learn) │
    └──────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────┐
    │                   LOOP C — ENRICH                        │
    │           (POST /api/enrich, jobs.enrichment stub)       │
    │                                                          │
    │  free text ──▶ llm.extract_events ──▶ geocode ──▶ upsert │
    │      │              │                       │       │    │
    │  "Golf in        structured              LocationIQ  venues│
    │   Hluboká        events                 (degrade{}) activities│
    │   19.6.2026"                                      goals  │
    └──────────────────────────────────────────────────────────┘
```

- **A — Plan**: `planner.build_and_send_plan` → gather conditions → score →
  route NOW/NEXT/PARK/ARCHIVE → Telegram plan with Approve / Reroll / Skip.
- **B — Observe**: enrich → `geo.segment_staypoints` over the unassigned ping
  tail → `llm.classify_staypoint` against nearby venues + today's approved plan
  → create session → `telegram.request_feedback` → ⭐ updates posterior.
- **C — Enrich**: free text → `llm.extract_events` → geocode → upsert
  venue/activity/goal. Scheduler job is still a stub for automated sources.

---

## Scoring Logic (The Heart)

```
                    ┌─────────────────────────────────┐
                    │   U(a | c, t) =                  │
                    │                                 │
                    │   ( w_pref·Pref                  │
                    │   + w_fit·Fit                    │
                    │   + w_prog·Prog                  │
                    │   + w_expl·Explore )             │
                    │                                 │
                    │   × rule_factor                  │
                    │                                 │
                    │   if blocked: U = 0              │
                    └─────────────────────────────────┘

  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
  │    Pref      │  │     Fit      │  │    Prog      │  │   Explore    │
  │              │  │              │  │              │  │              │
  │ Beta mean    │  │ [0,1] from   │  │ goal gap:    │  │ UCB term     │
  │ α/(α+β)      │  │ season ×     │  │ 1 - done/    │  │ √(2·lnN/nₐ)  │
  │              │  │ daylight ×   │  │   target     │  │              │
  │ Updated ONLY │  │ temp gates   │  │              │  │ select()     │
  │ by ⭐ rating │  │              │  │ planner.     │  │ reserves     │
  │              │  │ scoring.fit()│  │ _goal_gap()  │  │ explore_ratio│
  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │                    RULE FACTOR                                  │
  │                                                                 │
  │  For each rule matching active conditions (cond_type,val):      │
  │    block     → U = 0  (hard stop)                              │
  │    activate  → factor × weight                                 │
  │    modulate  → factor × weight                                 │
  │                                                                 │
  │  A single matching block rule ⇒ route PARK                      │
  └─────────────────────────────────────────────────────────────────┘
```

### Routing Decision Tree

```
                    ┌─────────────┐
                    │  activity   │
                    └──────┬──────┘
                           │
              ┌────────────▼────────────┐
              │  ≥5 samples AND pref<0.25?│
              └────────────┬────────────┘
                     yes /   \ no
                        /     \
                  ┌───▼───┐    │
                  │ARCHIVE│    │
                  └───────┘    │
                          ┌────▼─────┐
                          │ blocked? │
                          └────┬─────┘
                       yes /     \ no
                          /       \
                    ┌───▼───┐  ┌───▼────┐
                    │ PARK  │  │ fit≥0.6?│
                    └───────┘  └───┬─────┘
                            yes /   \ no
                              /       \
                        ┌───▼───┐ ┌───▼──────┐
                        │  NOW  │ │fit 0.2-0.6│
                        └───────┘ └─────┬────┘
                                        │
                                  ┌─────▼─────┐
                                  │   NEXT    │
                                  └───────────┘
                            (fit < 0.2 → PARK)
```

---

## Data Model & Flow

```
  GPS PHONE                EXTERNAL APIs              TELEGRAM
      │                        │                         │
      ▼                        ▼                         ▼
  ┌────────┐  ┌──────────────────────────┐     ┌──────────────┐
  │ /gps   │  │ enrich.py                │     │ telegram.py  │
  └───┬────┘  │  fetch_weather (cache)   │     │  send/receive│
      │       │  reverse_geocode         │     │  ⭐ → posterior│
      │       │  (both degrade to {})    │     └──────┬───────┘
      │       └──────────┬───────────────┘            │
      │                  │                            │
      ▼                  ▼                            │
  ╔══════════════════════════════════════════════╗    │
  ║              SQLite (kairos.db)              ║    │
  ║                                              ║    │
  ║  pings ──▶ sessions ──▶ feedback             ║◄───┘
  ║   │         │  │           │                 ║
  ║   │         │  ├──▶ venues │                 ║
  ║   │         │  │      │    │                 ║
  ║   │         │  │   is_anchor?──▶ sid=0       ║
  ║   │         │  │      │                      ║
  ║   │         │  └──▶ activities (α,β)         ║
  ║   │         │         │                      ║
  ║   │         │    rules (cond→act, kind)      ║
  ║   │         │         │                      ║
  ║   │         │      goals (cadence)           ║
  ║   │         │                                ║
  ║   │         └──▶ patterns (routes)           ║
  ║   │                                          ║
  ║   └─ sentinel 0 = anchor-suppressed          ║
  ║      NULL = still unassigned                 ║
  ║                                              ║
  ║  plans (JSON: conditions + ranked picks)     ║
  ╚══════════════════════════════════════════════╝
      │
      ▼
  ┌─────────────────────────────────┐
  │       FastAPI (one process)      │
  │  /dashboard  /tables  /decision  │
  │  /api/state  /api/{table}        │
  │  APScheduler (in-process)        │
  └─────────────────────────────────┘
```

### Tables

| Table | Purpose |
|---|---|
| `pings` | Raw GPS log; `session_id` set once assigned (NULL=unassigned, 0=anchor-suppressed) |
| `activities` | `alpha`/`beta` = Beta posterior over ⭐; `active=0` ⇒ ARCHIVED |
| `venues` | `is_anchor` excludes from matching |
| `rules` | The flattened causal edges |
| `sessions` | One classified staypoint |
| `feedback` | Star ratings |
| `goals` | Cadence targets |
| `plans` | `payload` is JSON: conditions + ranked picks + reasoning |
| `patterns` | Detected repeated origin→destination routes |

`pings.session_id` intentionally has **no FK** — the sentinel value `0` would
reject the write under a `REFERENCES` constraint.

---

## Module Boundaries (Dependency Rule)

```
          ┌──────────────────────────────────────┐
          │           app/main.py                │  ← all endpoints,
          │  (FastAPI, GPS pipeline, lifespan)   │    wiring, HTTP
          └──────┬──────────┬──────────┬─────────┘
                 │          │          │
          ┌──────▼──┐ ┌────▼───┐ ┌────▼────┐
          │ geo.py  │ │llm.py  │ │planner  │
          │ (math)  │ │(ONLY   │ │ .py     │
          │         │ │ LLM    │ │(logic)  │
          │ deter-  │ │ here + │ │         │
          │ ministic│ │fallbk) │ │ deter-  │
          └─────────┘ └────────┘ │ ministic│
                                └────┬────┘
                                     │
          ┌──────────┐         ┌─────▼─────┐
          │scoring.py│◄────────┤conditions │
          │ (the     │         │           │
          │  math)   │         └───────────┘
          └────┬─────┘
               │
     ┌─────────┼──────────┐
     │         │          │
┌────▼───┐ ┌───▼────┐ ┌───▼──────┐
│enrich  │ │telegram│ │ patterns │
│.py     │ │.py     │ │ .py      │
│(ext,   │ │(bot    │ │(determ.) │
│ {})    │ │ I/O)   │ │          │
└────────┘ └────────┘ └──────────┘

  Rule: dependencies point DOWN and INWARD.
  No module imports main.py. LLM never leaks out of llm.py.
  External I/O is isolated and null-safe.
```

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app, all endpoints, GPS→session pipeline, lifespan |
| `app/config.py` | env-driven `Settings` |
| `app/db.py` | SQLite connect + `init_db` (runs `schema.sql` then `seed.sql` + migrations) |
| `app/schema.sql` | DDL — all tables, `CREATE IF NOT EXISTS` (idempotent) |
| `app/seed.sql` | starter inventory, rules, home anchor, golf goal. `INSERT OR IGNORE` |
| `app/geo.py` | `haversine_m`, `segment_staypoints` |
| `app/scoring.py` | `Conditions`, `fit`, `rule_effect`, `utility`, `select`, `update_posterior`, UCB/Thompson, routing |
| `app/enrich.py` | OpenWeather + LocationIQ (weather cache, keyless fallback) |
| `app/llm.py` | `classify_staypoint`, `plan_rationale`, `extract_events`; heuristic fallbacks |
| `app/telegram.py` | send plan/feedback, parse webhook, log ⭐ → posterior |
| `app/planner.py` | `gather_conditions`, `_goal_gap`, `build_plan`, `build_and_send_plan` |
| `app/patterns.py` | `detect_patterns`, `upsert_patterns` (deterministic route detection) |
| `app/jobs.py` | APScheduler wiring for loops A & C + pattern detection + kairos window |

---

## Design Principles (How Decisions Get Made)

| Tension | KAIROS chooses... | Because... |
|---------|-------------------|------------|
| **Graph vs flat table** | Flat `rules` table + SQL filter | ≤2-hop logic doesn't need traversal; keeps it inspectable & editable |
| **LLM vs determinism** | Deterministic everywhere, LLM only in `llm.py` with fallback | Runs with zero keys; testable; cost predictable |
| **Sync vs async** | Async for network (httpx), sync for SQLite | SQLite ops are fast & short; personal scale |
| **Memory vs DB state** | State in DB, not in memory | Survives restarts; `_process_tail` recomputes from pings |
| **Real-time vs batch** | GPS is real-time (event), planning is scheduled | Pings must be acknowledged fast; plans are daily |
| **Edit by hand vs code** | Everything editable via `/tables` web UI | Inventory is personal data, not just config |
| **Block vs degrade** | External failures degrade to `{}`/`[]` | A missing API key must never crash the pipeline |
| **Approve vs auto** | Plans/venues go through human approval | The system suggests; the human decides |

---

## The Learning Loop (Only Place Preferences Change)

```
    Telegram ⭐ or /api/feedback
            │
            ▼
    ┌───────────────┐
    │ update_posterior│   stars 1-5 → success ∈ [0,1]
    │   (α, β, stars) │   α += s;  β += (1-s)
    └───────┬───────┘
            │
            ▼
    activities.alpha, activities.beta  (Beta posterior)
            │
            ▼
    Pref = α/(α+β)  feeds back into U(a|c,t) next plan

    ════════════════════════════════════════
    This is the ONLY place preferences change.
    Rules are NOT auto-learned (manual via /tables).
    Sessions feed goals.progress + patterns only.
    ════════════════════════════════════════
```

---

## Deployment Topology

Two containers in one compose stack: a `traefik` service terminates TLS (Let's
Encrypt via the TLS-ALPN challenge) and routes by `Host()` to the `app`
container over the internal Docker network.

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
(`127.0.0.1:8000:8000`) is loopback-only. uvicorn runs with `--proxy-headers`
to trust `X-Forwarded-*` from Traefik. Don't run a second app replica: SQLite +
the in-process scheduler assume a single writer.

The same single FastAPI app runs in both **development** (bare-metal, ngrok for
the public HTTPS that Telegram/Overland require) and **production** (Docker +
Traefik). The app code is identical; only the runtime differs.

---

*Every piece is deterministic except `llm.py`, every external call is null-safe,
and the entire system runs on one process with one SQLite file.*
