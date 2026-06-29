"""KAIROS — single-process FastAPI service.

Endpoints:
  POST /gps        Overland posts locations here (the only phone-side config).
  POST /tg         Telegram webhook (button taps + feedback replies).
  GET  /dashboard  Web dashboard.
  GET  /tables     Visual table editor (activities/venues/rules/goals).
  GET  /api/state  Dashboard data (JSON).
  GET  /admin      Lightweight status.
  GET  /health
  CRUD /api/{activities,venues,rules,goals}[/{id}]  Manage inventory tables.
"""
import json as _json
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .auth import (
    auth_enabled, check_credentials, get_session_secret,
    render_login_html, require_login,
)
from .db import get_conn, init_db
from .enrich import fetch_weather, reverse_geocode
from .geo import haversine_m, segment_staypoints
from .jobs import build_scheduler
from .llm import classify_staypoint
from . import telegram

scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(seed=True)
    global scheduler
    scheduler = build_scheduler()
    scheduler.start()
    # Register Telegram webhook if we know our public URL.
    if settings.telegram_bot_token and settings.public_base_url:
        await telegram._call(
            "setWebhook", {"url": f"{settings.public_base_url.rstrip('/')}/tg"}
        )
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="KAIROS", lifespan=lifespan)

# Cookie-session auth for the dashboard HTML pages. Gates ONLY /dashboard + /tables;
# /api/*, /admin, /gps, /tg, /health stay open. When DASHBOARD_PASSWORD is unset,
# auth is disabled (require_login short-circuits) and the app behaves as before.
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    session_cookie="kairos_session",
    same_site="lax",
    https_only=settings.session_cookie_secure,
    max_age=14 * 24 * 3600,
)


# ---------------------------- GPS ingest ----------------------------

@app.post("/gps")
async def gps(request: Request):
    body = await request.json()
    locations = body.get("locations", [])
    stored = 0
    for loc in locations:
        coords = loc.get("geometry", {}).get("coordinates", [None, None])
        props = loc.get("properties", {})
        lon, lat = coords[0], coords[-1]
        if lat is None or lon is None:
            continue
        ts = _parse_ts(props.get("timestamp"))
        weather = await fetch_weather(lat, lon)
        geo = await reverse_geocode(lat, lon)
        _insert_ping(lat, lon, ts, props, weather, geo)
        stored += 1

    if stored > 0:
        first_coords = locations[0].get("geometry", {}).get("coordinates", [None, None])
        _flon, _flat = first_coords[0], first_coords[-1]
        if _flat is not None:
            await telegram.trace(f"🛰️ GPS  {stored} ping(s)  ({_flat:.5f}, {_flon:.5f})")
    await _process_tail()
    if settings.debug_gps and stored > 0:
        with get_conn() as _c:
            # Last ping
            _p = _c.execute(
                "SELECT ts, lat, lon, address, weather_main, temp, session_id FROM pings ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            # Unassigned ping count (staypoint pipeline state)
            _unassigned = _c.execute("SELECT COUNT(*) n FROM pings WHERE session_id IS NULL").fetchone()["n"]
            # Nearest venue
            _venues = [dict(r) for r in _c.execute(
                "SELECT v.name, v.lat, v.lon, v.radius_m, a.name AS act FROM venues v LEFT JOIN activities a ON a.id=v.activity_id WHERE v.is_anchor=0"
            ).fetchall()]
            # Last session (if any was just created)
            _last_sess = _c.execute(
                "SELECT s.id, a.name AS act, s.confidence FROM sessions s LEFT JOIN activities a ON a.id=s.activity_id ORDER BY s.id DESC LIMIT 1"
            ).fetchone()

        _when = datetime.fromtimestamp(_p["ts"]).strftime("%H:%M:%S")
        _addr = _p["address"] or f"{_p['lat']:.5f}, {_p['lon']:.5f}"

        # Venue check
        _nearest = None
        _nearest_d = None
        for _v in _venues:
            _d = haversine_m(_p["lat"], _p["lon"], _v["lat"], _v["lon"])
            if _nearest is None or _d < _nearest_d:
                _nearest = _v
                _nearest_d = _d
        if _nearest and _nearest_d <= _nearest["radius_m"]:
            _venue_line = f"Venue match: {_nearest['name']} ({_nearest['act']}) {int(_nearest_d)}m"
        elif _nearest:
            _venue_line = f"No match — nearest: {_nearest['name']} {int(_nearest_d)}m away"
        else:
            _venue_line = "No venues configured"

        # Weather
        _wx = f"{_p['weather_main']}, {_p['temp']}°C" if _p["weather_main"] else "no weather"

        # Staypoint state
        _sp_line = f"Unassigned pings: {_unassigned} (need 12min+ dwell to close)"

        # Last session
        if _last_sess and _p["session_id"] and _p["session_id"] == _last_sess["id"]:
            _sess_line = f"New session: {_last_sess['act']} (conf {int((_last_sess['confidence'] or 0)*100)}%)"
        else:
            _sess_line = "No new session"

        await telegram.send_message(
            f"[GPS DEBUG] {stored} ping(s) at {_when}\n"
            f"Location: {_addr}\n"
            f"Weather: {_wx}\n"
            f"{_venue_line}\n"
            f"{_sp_line}\n"
            f"{_sess_line}"
        )
    return {"result": "ok"}


def _parse_ts(s: str | None) -> int:
    if not s:
        return int(time.time())
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return int(time.time())


def _insert_ping(lat, lon, ts, props, weather, geo) -> None:
    with get_conn() as conn:
        prev = conn.execute(
            "SELECT lat, lon, ts FROM pings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        dist = dur = None
        if prev:
            dist = round(haversine_m(prev["lat"], prev["lon"], lat, lon), 2)
            dur = float(ts - prev["ts"])
        conn.execute(
            """INSERT INTO pings
               (ts, lat, lon, altitude, wifi, address,
                weather_main, weather_desc, temp, pressure, humidity, wind_speed,
                distance_m, duration_s)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, lat, lon, props.get("altitude"), props.get("wifi"), geo.get("address"),
             weather.get("weather_main"), weather.get("weather_desc"), weather.get("temp"),
             weather.get("pressure"), weather.get("humidity"), weather.get("wind_speed"),
             dist, dur),
        )


# --------------------- staypoint -> session -> feedback ---------------------

async def _process_tail() -> None:
    """Run staypoint detection over unassigned pings; classify any that close."""
    with get_conn() as conn:
        pings = [
            dict(r) for r in conn.execute(
                "SELECT id, ts, lat, lon FROM pings WHERE session_id IS NULL ORDER BY ts ASC"
            ).fetchall()
        ]
    if len(pings) < 2:
        return

    await telegram.trace(f"🔍 Tail  {len(pings)} unassigned pings → staypoint scan")
    res = segment_staypoints(
        pings, settings.staypoint_radius_m, settings.staypoint_min_dwell_s
    )
    if res.closed:
        await telegram.trace(f"📍 {len(res.closed)} staypoint(s) closed, {len(res.open_tail)} pings still open")
    for sp in res.closed:
        await _materialize_session(sp)


async def _materialize_session(sp) -> None:
    with get_conn() as conn:
        venues = [dict(r) for r in conn.execute(
            """SELECT v.*, a.name AS activity_name
               FROM venues v LEFT JOIN activities a ON a.id = v.activity_id"""
        ).fetchall()]

    # nearest venues within radius, sorted by distance
    near = []
    for v in venues:
        d = haversine_m(sp.centroid_lat, sp.centroid_lon, v["lat"], v["lon"])
        if d <= v["radius_m"]:
            near.append((d, v))
    near.sort(key=lambda t: t[0])

    dwell_min = int(sp.dwell_s / 60)
    await telegram.trace(
        f"📍 Staypoint  {dwell_min}min dwell, {len(sp.point_ids)} pings"
        f" @ ({sp.centroid_lat:.4f}, {sp.centroid_lon:.4f})"
    )

    # anchor suppression: home dwell is not an activity
    if near and near[0][1]["is_anchor"]:
        await telegram.trace(
            f"🏠 Anchor suppressed  '{near[0][1]['name']}'  {dwell_min}min dwell"
        )
        _assign_pings(sp.point_ids, session_id=None, mark_done=True)
        return

    if near:
        best_d, best_v = near[0]
        await telegram.trace(
            f"🎯 Venue  '{best_v['name']}' ({best_v['activity_name']}) @ {int(best_d)}m"
        )
    else:
        await telegram.trace(
            f"❓ No venue within {int(settings.staypoint_radius_m)}m — classifying by position"
        )

    candidate_venues = [
        {**v, "distance_m": round(d, 1)} for d, v in near
    ]
    planned = _todays_planned_activities()
    cls = classify_staypoint(
        {"start_ts": sp.start_ts, "end_ts": sp.end_ts, "dwell_s": sp.dwell_s},
        candidate_venues, planned,
    )

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO sessions
               (start_ts, end_ts, duration_s, centroid_lat, centroid_lon,
                venue_id, activity_id, confidence)
               VALUES (?,?,?,?,?,?,?,?)""",
            (sp.start_ts, sp.end_ts, sp.dwell_s, sp.centroid_lat, sp.centroid_lon,
             cls.get("venue_id"), cls.get("activity_id"), cls.get("confidence")),
        )
        session_id = cur.lastrowid
        conn.executemany(
            "UPDATE pings SET session_id = ? WHERE id = ?",
            [(session_id, pid) for pid in sp.point_ids],
        )
        if cls.get("activity_id"):
            conn.execute(
                """UPDATE goals SET progress = (
                       SELECT COUNT(*) FROM sessions
                       WHERE activity_id = goals.activity_id
                         AND end_ts >= (CAST(strftime('%s','now') AS INTEGER) - cadence_days * 86400)
                   ) WHERE activity_id = ?""",
                (cls["activity_id"],),
            )
        act = None
        if cls.get("activity_id"):
            act = conn.execute(
                "SELECT name FROM activities WHERE id = ?", (cls["activity_id"],)
            ).fetchone()

    act_label = act["name"] if act else "unknown"
    conf_pct = int((cls.get("confidence") or 0) * 100)
    when = (
        f"{datetime.fromtimestamp(sp.start_ts).strftime('%H:%M')}–"
        f"{datetime.fromtimestamp(sp.end_ts).strftime('%H:%M')}"
    )
    await telegram.trace(
        f"✅ Session #{session_id}  {act_label}  {when}  conf={conf_pct}%"
    )
    if act:
        await telegram.request_feedback(session_id, act["name"], when)
    elif not near:
        # No venue matched — ask user to identify this new location
        await telegram.request_unknown_venue(session_id, sp.centroid_lat, sp.centroid_lon, dwell_min)


def _assign_pings(point_ids, session_id, mark_done=False) -> None:
    # Anchor dwells get a sentinel session_id (0) so they aren't re-processed.
    sid = 0 if mark_done else session_id
    with get_conn() as conn:
        conn.executemany(
            "UPDATE pings SET session_id = ? WHERE id = ?",
            [(sid, pid) for pid in point_ids],
        )


def _todays_planned_activities() -> list[str]:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload FROM plans WHERE date = ? AND status = 'approved' "
            "ORDER BY created_ts DESC LIMIT 1",
            (today,),
        ).fetchone()
    if not row:
        return []
    import json
    return [p["name"] for p in json.loads(row["payload"]).get("picks", [])]


# ------------------------------ Telegram ------------------------------

@app.post("/tg")
async def telegram_webhook(request: Request):
    await telegram.handle_update(await request.json())
    return {"ok": True}


# ------------------------------ admin ------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin")
async def admin():
    with get_conn() as conn:
        pings = conn.execute("SELECT COUNT(*) n FROM pings").fetchone()["n"]
        sessions = conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
        acts = [dict(r) for r in conn.execute(
            "SELECT name, cluster, alpha, beta, active FROM activities").fetchall()]
        for a in acts:
            a["pref"] = round(a["alpha"] / (a["alpha"] + a["beta"]), 3)
    return {"pings": pings, "sessions": sessions, "activities": acts}


# ------------------------------ DB export / import ------------------------------

@app.get("/api/db/export")
async def db_export():
    db = Path(settings.db_path)
    if not db.exists():
        raise HTTPException(404, "Database file not found")
    return FileResponse(path=str(db), media_type="application/octet-stream", filename="kairos.db")


@app.post("/api/db/import")
async def db_import(file: UploadFile = File(...)):
    db_path = Path(settings.db_path)
    tmp_path = db_path.with_suffix(".import_tmp")
    content = await file.read()
    try:
        tmp_path.write_bytes(content)
        try:
            chk = sqlite3.connect(str(tmp_path))
            result = chk.execute("PRAGMA integrity_check").fetchone()[0]
            chk.close()
        except Exception as e:
            raise HTTPException(400, f"Invalid SQLite file: {e}")
        if result != "ok":
            raise HTTPException(400, f"Integrity check failed: {result}")
        tmp_path.replace(db_path)
    except HTTPException:
        raise
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return {"status": "ok", "bytes": len(content)}


# ------------------------------ dashboard ------------------------------

@app.get("/api/state")
async def api_state():
    from .planner import gather_conditions, _goal_gap
    from .scoring import utility
    c = gather_conditions()

    with get_conn() as conn:
        today = datetime.now().strftime("%Y-%m-%d")
        plan_row = conn.execute(
            "SELECT * FROM plans WHERE date = ? "
            "ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END, created_ts DESC LIMIT 1",
            (today,),
        ).fetchone()
        current_plan = None
        if plan_row:
            p = dict(plan_row)
            payload = _json.loads(p["payload"])
            current_plan = {
                "id": p["id"], "date": p["date"], "horizon": p["horizon"],
                "status": p["status"], "created_ts": p["created_ts"],
                "summary": payload.get("summary", ""),
                "picks": payload.get("picks", []),
                "ranked": payload.get("ranked", []),
                "conditions": payload.get("conditions", {}),
            }

        recent_plans = []
        for r in conn.execute(
            "SELECT id, date, horizon, status, payload, created_ts FROM plans ORDER BY created_ts DESC LIMIT 10"
        ).fetchall():
            rp = dict(r)
            pl = _json.loads(rp.pop("payload"))
            rp["summary"] = pl.get("summary", "")
            rp["picks"] = pl.get("picks", [])
            recent_plans.append(rp)

        sessions = [dict(r) for r in conn.execute(
            """SELECT s.id, s.start_ts, s.end_ts, s.duration_s, s.confidence,
                      s.feedback_requested, s.centroid_lat, s.centroid_lon,
                      a.name AS activity, v.name AS venue,
                      f.stars, f.note, f.emotion
               FROM sessions s
               LEFT JOIN activities a ON a.id = s.activity_id
               LEFT JOIN venues v ON v.id = s.venue_id
               LEFT JOIN feedback f ON f.session_id = s.id
               ORDER BY s.start_ts DESC LIMIT 15"""
        ).fetchall()]

        pings = [dict(r) for r in conn.execute(
            "SELECT id, ts, lat, lon, address, weather_main, temp, session_id "
            "FROM pings ORDER BY ts DESC LIMIT 30"
        ).fetchall()]

        activities_raw = [dict(r) for r in conn.execute("SELECT * FROM activities").fetchall()]
        rules = [dict(r) for r in conn.execute("SELECT * FROM rules").fetchall()]
        n_total = conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
        counts = {
            r["activity_id"]: r["n"]
            for r in conn.execute(
                "SELECT activity_id, COUNT(*) n FROM sessions GROUP BY activity_id"
            ).fetchall()
        }
        activities = []
        for a in activities_raw:
            sc = utility(a, c, rules,
                         n_a=counts.get(a["id"], 0), n_total=n_total,
                         goal_gap=_goal_gap(a["id"], conn))
            activities.append({
                "id": a["id"], "name": a["name"], "cluster": a["cluster"],
                "pref": round(a["alpha"] / (a["alpha"] + a["beta"]), 3),
                "alpha": round(a["alpha"], 2), "beta": round(a["beta"], 2),
                "active": a["active"], "utility": sc.utility,
                "fit": sc.fit, "explore": sc.explore,
                "route": sc.route, "blocked": sc.blocked, "reason": sc.reason,
                "causal_chain": sc.causal_chain,
            })
        activities.sort(key=lambda x: x["utility"], reverse=True)

        feedback = [dict(r) for r in conn.execute(
            """SELECT f.stars, f.note, f.emotion, f.ts, a.name AS activity
               FROM feedback f JOIN sessions s ON s.id = f.session_id
               LEFT JOIN activities a ON a.id = s.activity_id
               ORDER BY f.ts DESC LIMIT 20"""
        ).fetchall()]

        venues = [dict(r) for r in conn.execute(
            """SELECT v.id, v.name, v.lat, v.lon, v.radius_m, v.is_anchor, v.address,
                      a.name AS activity
               FROM venues v LEFT JOIN activities a ON a.id = v.activity_id
               ORDER BY v.is_anchor DESC, v.id"""
        ).fetchall()]

        goals = [dict(r) for r in conn.execute(
            """SELECT g.id, g.name, g.metric, g.target, g.cadence_days, g.progress,
                      a.name AS activity
               FROM goals g LEFT JOIN activities a ON a.id = g.activity_id
               ORDER BY g.id"""
        ).fetchall()]

    return {
        "conditions": {
            "season": c.season, "daytime": c.daytime,
            "temp": c.temp, "weather": c.weather_main,
        },
        "current_plan": current_plan,
        "recent_plans": recent_plans,
        "sessions": sessions,
        "pings": pings,
        "activities": activities,
        "feedback": feedback,
        "venues": venues,
        "goals": goals,
    }


# ------------------------------ auth (dashboard login) ------------------------------

@app.get("/login")
async def login_form():
    if not auth_enabled():
        return RedirectResponse("/dashboard", status_code=303)
    return HTMLResponse(render_login_html())


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not auth_enabled():
        return RedirectResponse("/dashboard", status_code=303)
    if check_credentials(username, password):
        request.session["user"] = settings.dashboard_username
        return RedirectResponse("/dashboard", status_code=303)
    return HTMLResponse(render_login_html("Invalid username or password."), status_code=401)


@app.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request):
    # Both verbs so a header <a href="/logout"> link works without JS.
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------ dashboard ------------------------------

@app.get("/dashboard")
async def dashboard(_: None = Depends(require_login)):
    html = Path(__file__).with_name("dashboard.html").read_text()
    return HTMLResponse(html)


@app.get("/tables")
async def tables_page(_: None = Depends(require_login)):
    html = Path(__file__).with_name("tables.html").read_text()
    return HTMLResponse(html)


# ------------------------------ enrichment (Loop C) ------------------------------

@app.post("/api/enrich")
async def enrich(request: Request):
    """Accept free text, extract events via LLM, geocode venues, insert into DB.

    Returns the list of inserted records so the caller (dashboard or Telegram)
    can confirm what was written.
    """
    from .llm import extract_events
    from .enrich import reverse_geocode
    import httpx as _httpx

    body = await request.json()
    text = str(body.get("text", "")).strip()
    if not text:
        return {"error": "text required"}

    with get_conn() as conn:
        known = [r["name"] for r in conn.execute("SELECT name FROM activities").fetchall()]

    proposals = extract_events(text, known)
    if not proposals:
        return {"inserted": [], "note": "LLM returned no proposals (check ANTHROPIC_API_KEY or rephrase)."}

    inserted = []
    for p in proposals:
        venue_name = p.get("venue_name", "").strip()
        geocode_q = p.get("geocode_query", venue_name)
        activity_name = (p.get("activity") or "").strip().lower()
        cluster = p.get("cluster", "SPORT") if p.get("cluster") in ("SPORT","TRIP","RECOVERY","CULTURE","SOCIAL") else "SPORT"
        event_date = p.get("event_date")
        notes = p.get("notes", "")

        if not venue_name or not activity_name:
            continue

        # Geocode the venue
        lat = lon = address = None
        if settings.locationiq_api_key and geocode_q:
            try:
                async with _httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://us1.locationiq.com/v1/search",
                        params={"key": settings.locationiq_api_key, "q": geocode_q, "format": "json", "limit": 1},
                    )
                    r.raise_for_status()
                    hits = r.json()
                    if hits:
                        lat = float(hits[0]["lat"])
                        lon = float(hits[0]["lon"])
                        address = hits[0].get("display_name")
            except Exception:
                pass

        with get_conn() as conn:
            # Upsert activity
            conn.execute(
                "INSERT OR IGNORE INTO activities (name, cluster) VALUES (?, ?)",
                (activity_name, cluster),
            )
            act_id = conn.execute(
                "SELECT id FROM activities WHERE name = ?", (activity_name,)
            ).fetchone()["id"]

            # Only insert venue if geocoding succeeded — 0,0 would never match GPS
            venue_id = None
            if venue_name and lat is not None and lon is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO venues (name, lat, lon, radius_m, activity_id, address) VALUES (?,?,?,?,?,?)",
                    (venue_name, lat, lon, 300, act_id, address),
                )
                venue_id = conn.execute(
                    "SELECT id FROM venues WHERE name = ?", (venue_name,)
                ).fetchone()["id"]

            # If there's an event date, add a one-off goal with 1-session target
            goal_id = None
            if event_date and venue_id:
                goal_name = f"{activity_name}-{event_date}"
                conn.execute(
                    "INSERT OR IGNORE INTO goals (name, activity_id, metric, target, cadence_days) VALUES (?,?,?,?,?)",
                    (goal_name, act_id, "sessions", 1, 1),
                )
                goal_id = conn.execute(
                    "SELECT id FROM goals WHERE name = ?", (goal_name,)
                ).fetchone()["id"]

        geocode_ok = lat is not None and lon is not None
        inserted.append({
            "venue": venue_name, "lat": lat, "lon": lon,
            "geocoded": geocode_ok,
            "activity": activity_name, "cluster": cluster,
            "event_date": event_date, "notes": notes,
            "activity_id": act_id, "venue_id": venue_id, "goal_id": goal_id,
            "warning": None if geocode_ok else "Venue not geocoded — fix LOCATIONIQ_API_KEY and re-submit to add GPS matching.",
        })

    return {"inserted": inserted}


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Log a star rating for a session and update the Beta posterior."""
    from .scoring import update_posterior
    body = await request.json()
    session_id = body.get("session_id")
    stars = int(body.get("stars", 0))
    note = str(body.get("note", "")).strip()
    emotion = str(body.get("emotion", "")).strip() or None
    if not session_id or not (1 <= stars <= 5):
        return {"error": "session_id and stars (1-5) required"}
    with get_conn() as conn:
        row = conn.execute(
            "SELECT s.activity_id, a.alpha, a.beta FROM sessions s "
            "JOIN activities a ON a.id = s.activity_id WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return {"error": "session not found or has no activity"}
        already = conn.execute(
            "SELECT id FROM feedback WHERE session_id = ?", (session_id,)
        ).fetchone()
        if already:
            return {"error": "feedback already logged for this session"}
        conn.execute(
            "INSERT INTO feedback (session_id, stars, note, emotion, ts) VALUES (?,?,?,?,?)",
            (session_id, stars, note or None, emotion, int(time.time())),
        )
        new_a, new_b = update_posterior(row["alpha"], row["beta"], stars)
        conn.execute(
            "UPDATE activities SET alpha = ?, beta = ? WHERE id = ?",
            (new_a, new_b, row["activity_id"]),
        )
        conn.execute(
            "UPDATE sessions SET feedback_requested = 0 WHERE id = ?", (session_id,)
        )
    return {"ok": True, "new_pref": round(new_a / (new_a + new_b), 3)}


# ------------------------------ venue auto-create from sessions ------------------------------

@app.post("/api/import/create-venues-from-sessions")
async def create_venues_from_sessions():
    """For each session with an activity but no nearby venue, create a venue at the centroid.

    This closes the GPS matching gap: activities imported from CSV have sessions but no venues,
    so live Overland pings near those locations can't be auto-classified. After running this,
    future staypoints will match the auto-created venues and trigger feedback requests.
    """
    with get_conn() as conn:
        sessions = [dict(r) for r in conn.execute(
            """SELECT s.id, s.centroid_lat, s.centroid_lon, s.activity_id, a.name AS act_name
               FROM sessions s JOIN activities a ON a.id = s.activity_id
               WHERE s.activity_id IS NOT NULL"""
        ).fetchall()]
        venues = [dict(r) for r in conn.execute(
            "SELECT id, lat, lon, activity_id FROM venues WHERE is_anchor = 0"
        ).fetchall()]

    venues_created = 0
    for s in sessions:
        matched = any(
            haversine_m(s["centroid_lat"], s["centroid_lon"], v["lat"], v["lon"]) <= 300
            for v in venues if v["activity_id"] == s["activity_id"]
        )
        if not matched:
            vname = f"{s['act_name']} (auto)"
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO venues (name, lat, lon, radius_m, activity_id) VALUES (?,?,?,?,?)",
                    (vname, s["centroid_lat"], s["centroid_lon"], 200, s["activity_id"]),
                )
                new_v = conn.execute(
                    "SELECT id, lat, lon, activity_id FROM venues WHERE rowid = last_insert_rowid()"
                ).fetchone()
            venues.append(dict(new_v))
            venues_created += 1

    return {"venues_created": venues_created}


# ------------------------------ activity edit ------------------------------

_VALID_CLUSTERS = {
    "SPORT", "CULTURE", "RELAX", "RECOVERY", "STUDY",
    "WORK", "LIFE ADMIN", "FAMILY / CARE", "HEALTH / WELLNESS",
    "TRIP", "SOCIAL",  # legacy
}


@app.patch("/api/activity/{activity_id}")
async def patch_activity(activity_id: int, request: Request):
    """Update cluster and/or active flag for an activity."""
    body = await request.json()
    allowed = {"cluster", "active"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        return {"error": "nothing to update; allowed fields: cluster, active"}
    if "cluster" in updates and updates["cluster"] not in _VALID_CLUSTERS:
        return {"error": f"cluster must be one of {sorted(_VALID_CLUSTERS)}"}
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE activities SET {set_clause} WHERE id = ?",
            (*updates.values(), activity_id),
        )
        row = conn.execute(
            "SELECT id, name, cluster, active FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
    if not row:
        return {"error": "activity not found"}
    return dict(row)


# ------------------------------ generic table CRUD ------------------------------
# Covers the inventory tables a person actually hand-edits. Pipeline tables
# (pings, sessions, feedback, plans) are written by the GPS/scoring pipeline
# and are intentionally not exposed here.

_CRUD_TABLES = {
    "activities": {
        "columns": {
            "name", "cluster", "alpha", "beta", "daylight_required",
            "t_min", "t_max", "season_mask", "typical_duration_min", "active",
        },
        "required": {"name", "cluster"},
    },
    "venues": {
        "columns": {"name", "lat", "lon", "radius_m", "activity_id", "is_anchor", "address"},
        "required": {"name", "lat", "lon"},
    },
    "rules": {
        "columns": {"cond_type", "cond_value", "activity_id", "kind", "weight"},
        "required": {"cond_type", "cond_value", "activity_id", "kind"},
    },
    "goals": {
        "columns": {"name", "activity_id", "metric", "target", "cadence_days", "progress"},
        "required": {"name", "target"},
    },
}

_CRUD_ENUMS = {
    "activities": {"cluster": _VALID_CLUSTERS},
    "rules": {"kind": {"activate", "block", "modulate"}},
}


def _crud_spec(table: str) -> dict:
    spec = _CRUD_TABLES.get(table)
    if spec is None:
        raise HTTPException(404, f"unknown table '{table}'; one of {sorted(_CRUD_TABLES)}")
    return spec


def _crud_validate(table: str, fields: dict) -> None:
    for field, allowed in _CRUD_ENUMS.get(table, {}).items():
        if field in fields and fields[field] not in allowed:
            raise HTTPException(400, f"{field} must be one of {sorted(allowed)}")


@app.get("/api/{table}")
async def crud_list(table: str):
    _crud_spec(table)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/{table}/{row_id}")
async def crud_get(table: str, row_id: int):
    _crud_spec(table)
    with get_conn() as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"{table[:-1]} {row_id} not found")
    return dict(row)


@app.post("/api/{table}")
async def crud_create(table: str, request: Request):
    spec = _crud_spec(table)
    body = await request.json()
    missing = spec["required"] - body.keys()
    if missing:
        raise HTTPException(400, f"missing required fields: {sorted(missing)}")
    fields = {k: v for k, v in body.items() if k in spec["columns"]}
    _crud_validate(table, fields)
    if table == "venues" and "address" not in fields:
        fields["address"] = (await reverse_geocode(fields["lat"], fields["lon"])).get("address")
    cols = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    try:
        with get_conn() as conn:
            cur = conn.execute(
                f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, str(e))
    return dict(row)


@app.put("/api/{table}/{row_id}")
async def crud_update(table: str, row_id: int, request: Request):
    spec = _crud_spec(table)
    body = await request.json()
    fields = {k: v for k, v in body.items() if k in spec["columns"]}
    if not fields:
        raise HTTPException(400, f"nothing to update; allowed fields: {sorted(spec['columns'])}")
    _crud_validate(table, fields)
    if table == "venues" and "address" not in fields and ("lat" in fields or "lon" in fields):
        with get_conn() as conn:
            current = conn.execute("SELECT lat, lon FROM venues WHERE id = ?", (row_id,)).fetchone()
        if current:
            lat = fields.get("lat", current["lat"])
            lon = fields.get("lon", current["lon"])
            fields["address"] = (await reverse_geocode(lat, lon)).get("address")
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    try:
        with get_conn() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET {set_clause} WHERE id = ?",
                (*fields.values(), row_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(404, f"{table[:-1]} {row_id} not found")
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, str(e))
    return dict(row)


@app.delete("/api/{table}/{row_id}")
async def crud_delete(table: str, row_id: int):
    _crud_spec(table)
    try:
        with get_conn() as conn:
            # Sessions are a historical log, not config — unlink rather than
            # block deletion of the venue/activity they once pointed to.
            if table == "venues":
                conn.execute("UPDATE sessions SET venue_id = NULL WHERE venue_id = ?", (row_id,))
            elif table == "activities":
                conn.execute("UPDATE sessions SET activity_id = NULL WHERE activity_id = ?", (row_id,))
                conn.execute("UPDATE venues SET activity_id = NULL WHERE activity_id = ?", (row_id,))
                # rules.activity_id is NOT NULL — a rule without its activity
                # is meaningless, so cascade rather than block or null it.
                conn.execute("DELETE FROM rules WHERE activity_id = ?", (row_id,))
            cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, str(e))
    if cur.rowcount == 0:
        raise HTTPException(404, f"{table[:-1]} {row_id} not found")
    return {"deleted": row_id}


# ------------------------------ GPS CSV import ------------------------------

# Category → (activity_name, cluster)
_CATEGORY_MAP = {
    "swimming":      ("cold-water swimming", "SPORT"),
    "entertainment": ("entertainment",       "CULTURE"),
    "coffee shop":   ("cafe",                "CULTURE"),
    "shopping":      ("shopping",            "SOCIAL"),
    "sport":         ("sport",               "SPORT"),
    "culture":       ("culture",             "CULTURE"),
    "restaurant":    ("dining",              "SOCIAL"),
}
_SKIP_ACTIONS    = {"home", "road", ""}
_SKIP_CATEGORIES = {"transport", ""}


@app.post("/api/import/gps-csv")
async def import_gps_csv():
    """Import GPS records.csv from the project root.

    Idempotent: rows whose createdAt timestamp already exists in pings are skipped.
    Rows with Stars > 0 and non-transport/non-home category also create a session + feedback.
    Stars are on a 0-10 scale in the CSV; we normalise to 1-5.
    """
    import csv as _csv
    from .scoring import update_posterior

    csv_path = Path(__file__).parent.parent / "GPS records.csv"
    if not csv_path.exists():
        return {"error": f"File not found: {csv_path}"}

    # Collect already-imported timestamps to make the endpoint idempotent
    with get_conn() as conn:
        existing_ts = {
            r[0] for r in conn.execute("SELECT ts FROM pings").fetchall()
        }

    pings_imported = sessions_created = feedback_created = skipped = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = _csv.DictReader(f)
        for row in reader:
            # Parse timestamp from createdAt
            created_raw = row.get("createdAt", "").strip()
            if not created_raw:
                skipped += 1
                continue
            try:
                ts = int(datetime.fromisoformat(
                    created_raw.replace("Z", "+00:00")
                ).timestamp())
            except Exception:
                skipped += 1
                continue

            # Parse coordinates
            try:
                lat = float(row["lat"]) if row.get("lat") else None
                lon = float(row["lon"]) if row.get("lon") else None
            except (ValueError, KeyError):
                lat = lon = None

            if lat is None or lon is None:
                skipped += 1
                continue

            # Skip already-imported timestamps
            if ts in existing_ts:
                skipped += 1
                continue

            action   = (row.get("Action")   or "").strip()
            category = (row.get("Category") or "").strip()
            address  = (row.get("Address")  or "").strip()
            note     = (row.get("Note")     or "").strip() or None
            emotion  = (row.get("Emotion")  or "").strip() or None

            stars_raw = row.get("Stars", "0").strip()
            try:
                stars_10 = int(float(stars_raw))
            except (ValueError, TypeError):
                stars_10 = 0

            weather_main = row.get("Weather_main", "").strip() or None
            weather_desc = row.get("Weather_desc", "").strip() or None
            try:
                temp      = float(row["Temp"])      if row.get("Temp")      else None
                pressure  = float(row["Pressure"])  if row.get("Pressure")  else None
                humidity  = float(row["Humidity"])  if row.get("Humidity")  else None
                wind_speed= float(row["Wind_speed"])if row.get("Wind_speed")else None
            except (ValueError, KeyError):
                temp = pressure = humidity = wind_speed = None

            # Determine if this is a home/anchor dwell
            is_anchor = action.lower() == "home"
            is_transport = (
                category.lower() in _SKIP_CATEGORIES or
                action.lower() in {"road", "transport"}
            )

            # Insert ping
            sid = 0 if is_anchor else None
            with get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO pings
                       (ts, lat, lon, address, weather_main, weather_desc,
                        temp, pressure, humidity, wind_speed, session_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (ts, lat, lon, address or None, weather_main, weather_desc,
                     temp, pressure, humidity, wind_speed, sid),
                )
            existing_ts.add(ts)
            pings_imported += 1

            # Create session + feedback for rated non-transport, non-home rows
            if stars_10 > 0 and not is_anchor and not is_transport and action.lower() not in _SKIP_ACTIONS:
                stars_5 = max(1, min(5, round(stars_10 / 2)))

                # Resolve activity
                cat_key = category.lower()
                if cat_key in _CATEGORY_MAP:
                    act_name, cluster = _CATEGORY_MAP[cat_key]
                else:
                    act_name = action.lower() if action else cat_key or "unknown"
                    cluster  = "CULTURE"

                try:
                    duration_s = float(row.get("Duration") or 0) * 60
                except (ValueError, TypeError):
                    duration_s = 0.0
                if duration_s < 60:
                    duration_s = 1800.0  # default 30 min

                start_ts = ts - int(duration_s)
                end_ts   = ts

                with get_conn() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO activities (name, cluster) VALUES (?,?)",
                        (act_name, cluster),
                    )
                    act_id = conn.execute(
                        "SELECT id FROM activities WHERE name = ?", (act_name,)
                    ).fetchone()["id"]

                    cur = conn.execute(
                        """INSERT INTO sessions
                           (start_ts, end_ts, duration_s, centroid_lat, centroid_lon,
                            activity_id, confidence, feedback_requested)
                           VALUES (?,?,?,?,?,?,?,0)""",
                        (start_ts, end_ts, duration_s, lat, lon, act_id, 0.9),
                    )
                    session_id = cur.lastrowid

                    # Update ping to point at this session
                    conn.execute(
                        "UPDATE pings SET session_id = ? WHERE ts = ? AND lat = ?",
                        (session_id, ts, lat),
                    )

                    row_act = conn.execute(
                        "SELECT alpha, beta FROM activities WHERE id = ?", (act_id,)
                    ).fetchone()
                    new_a, new_b = update_posterior(row_act["alpha"], row_act["beta"], stars_5)
                    conn.execute(
                        "UPDATE activities SET alpha = ?, beta = ? WHERE id = ?",
                        (new_a, new_b, act_id),
                    )

                    conn.execute(
                        "INSERT INTO feedback (session_id, stars, note, emotion, ts) VALUES (?,?,?,?,?)",
                        (session_id, stars_5, note, emotion, ts),
                    )
                    conn.execute(
                        """UPDATE goals SET progress = (
                               SELECT COUNT(*) FROM sessions
                               WHERE activity_id = goals.activity_id
                                 AND end_ts >= (CAST(strftime('%s','now') AS INTEGER) - cadence_days * 86400)
                           ) WHERE activity_id = ?""",
                        (act_id,),
                    )

                sessions_created += 1
                feedback_created += 1

    return {
        "pings_imported":   pings_imported,
        "sessions_created": sessions_created,
        "feedback_created": feedback_created,
        "skipped":          skipped,
    }
