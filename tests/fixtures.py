#!/usr/bin/env python3
"""
Load sample records for every KAIROS use case.
Run: .venv/bin/python3 tests/fixtures.py

Idempotent: sessions/pings/plans are only inserted when the table is empty
(to avoid duplicates on re-runs). Activities, venues, rules, and goals use
INSERT OR IGNORE / existence checks.

Use cases exercised after loading:
  A  Planning       — approved daily plan + ranked utilities visible on /dashboard
  B1 GPS → staypoint — 15 unassigned pings at Podolí pool; send one more GPS ping
                       to trigger _process_tail() and watch the session appear
  B2 Anchor suppression — 5 home pings already carry session_id=0
  B3 Feedback loop  — 1 open session (feedback_requested=1) awaits ⭐ rating
  B4 Posterior learning — activities have varied alpha/beta from 16 past sessions
  C  Goal tracking  — 5 goals with live progress from history
"""
import json
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "kairos.db"


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _ago(days: float = 0, hours: float = 0, hour: int | None = None) -> datetime:
    base = datetime.now() - timedelta(days=days, hours=hours)
    if hour is not None:
        base = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    return base


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys = ON")

    def act_id(name: str) -> int | None:
        r = con.execute("SELECT id FROM activities WHERE name=?", (name,)).fetchone()
        return r["id"] if r else None

    def ven_id(name: str) -> int | None:
        r = con.execute("SELECT id FROM venues WHERE name=?", (name,)).fetchone()
        return r["id"] if r else None

    def ven_coords(vid: int | None) -> tuple[float, float]:
        if vid is None:
            return 50.07, 14.42
        r = con.execute("SELECT lat, lon FROM venues WHERE id=?", (vid,)).fetchone()
        return (r["lat"], r["lon"]) if r else (50.07, 14.42)

    print(f"Loading fixtures → {DB_PATH}\n")

    # ── 1. Extra activities ──────────────────────────────────────────────────
    con.executemany(
        "INSERT OR IGNORE INTO activities"
        " (name, cluster, daylight_required, t_min, t_max, season_mask, typical_duration_min)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("sauna",  "RECOVERY", 0, -50, 50, "spring,summer,autumn,winter", 60),
            ("yoga",   "RECOVERY", 0, -50, 50, "spring,summer,autumn,winter", 60),
            ("museum", "CULTURE",  1,  -5, 35, "spring,summer,autumn,winter", 120),
        ],
    )
    print("activities    ✓")

    # ── 2. Posteriors — simulate learned preferences from past sessions ───────
    # Only update where still at the uniform prior (1.0 / 1.0) so real feedback
    # from the UI is not overwritten.
    posteriors = {
        "golf":                (18.0, 4.0),   # strongly preferred, played often
        "cycling":             (14.0, 3.0),   # well liked
        "cold-water swimming": (10.0, 2.0),   # liked, less frequent
        "hike":                (8.0,  3.0),   # moderately liked
        "swimming":            (6.0,  6.0),   # neutral
        "massage":             (5.0,  2.0),   # liked, infrequent
        "reading-spot":        (4.0,  5.0),   # slightly below neutral
        "sauna":               (3.0,  1.5),   # liked but new
        "yoga":                (2.0,  3.0),   # tried, mild dislike
        "museum":              (2.0,  4.0),   # not preferred
    }
    for name, (a, b) in posteriors.items():
        con.execute(
            "UPDATE activities SET alpha=?, beta=? WHERE name=? AND alpha=1.0 AND beta=1.0",
            (a, b, name),
        )
    print("posteriors    ✓")

    # ── 3. Venues (Prague real coordinates) ──────────────────────────────────
    venue_rows = [
        ("Golf Hodkovičky",                    50.0362, 14.4092, 300, "golf",                "Praha 4, CZ"),
        ("Plavecký stadion Podolí",            50.0592, 14.4128, 200, "swimming",            "Podolí, Praha 4, CZ"),
        ("Vltava cold swim – Císařský ostrov", 50.1180, 14.4445, 250, "cold-water swimming", "Císařský ostrov, Praha, CZ"),
        ("Prokopské údolí trailhead",          50.0402, 14.3595, 300, "hike",                "Prokopské údolí, Praha 5, CZ"),
        ("Cycling start – Vltava riverbank",   49.9680, 14.3900, 300, "cycling",             "Zbraslav, Praha, CZ"),
        ("Massage Studio – Centrum",           50.0803, 14.4296, 100, "massage",             "Staré Město, Praha 1, CZ"),
        ("Café Slavia",                        50.0805, 14.4139, 120, "reading-spot",        "Smetanovo nábřeží, Praha 1, CZ"),
        ("Sauna Žluté lázně",                  50.0525, 14.4165, 150, "sauna",               "Braník, Praha 4, CZ"),
        ("Yoga studio – Vinohrady",            50.0752, 14.4357, 100, "yoga",                "Vinohrady, Praha 2, CZ"),
        ("National Museum",                    50.0756, 14.4310, 200, "museum",              "Václavské náměstí, Praha 1, CZ"),
    ]
    for vname, vlat, vlon, vrad, act_name, addr in venue_rows:
        if not con.execute("SELECT 1 FROM venues WHERE name=?", (vname,)).fetchone():
            con.execute(
                "INSERT INTO venues (name, lat, lon, radius_m, activity_id, address)"
                " VALUES (?,?,?,?,?,?)",
                (vname, vlat, vlon, vrad, act_id(act_name), addr),
            )
    print("venues        ✓")

    # ── 4. Extra rules ────────────────────────────────────────────────────────
    extra_rules = [
        # Season blocks
        ("season",  "winter",       "swimming",            "block",    1.0),
        ("season",  "winter",       "hike",                "block",    1.0),
        ("season",  "summer",       "cold-water swimming", "block",    1.0),
        # Severe weather blocks
        ("weather", "Thunderstorm", "golf",                "block",    1.0),
        ("weather", "Thunderstorm", "cycling",             "block",    1.0),
        ("weather", "Thunderstorm", "hike",                "block",    1.0),
        # Good-weather boosts
        ("weather", "Clear",        "cycling",             "activate", 1.2),
        ("weather", "Clear",        "hike",                "activate", 1.1),
        # Daylight boosts
        ("daytime", "daylight",     "swimming",            "activate", 1.1),
        ("daytime", "daylight",     "museum",              "activate", 1.2),
    ]
    for cond_type, cond_value, act_name, kind, weight in extra_rules:
        aid = act_id(act_name)
        if aid and not con.execute(
            "SELECT 1 FROM rules WHERE cond_type=? AND cond_value=? AND activity_id=? AND kind=?",
            (cond_type, cond_value, aid, kind),
        ).fetchone():
            con.execute(
                "INSERT INTO rules (cond_type, cond_value, activity_id, kind, weight)"
                " VALUES (?,?,?,?,?)",
                (cond_type, cond_value, aid, kind, weight),
            )
    print("rules         ✓")

    # ── 5. Extra goals ────────────────────────────────────────────────────────
    extra_goals = [
        ("cycling-weekly",    "cycling",             "sessions", 2,  7),
        ("swim-monthly",      "swimming",            "sessions", 3, 30),
        ("recovery-weekly",   "massage",             "sessions", 1,  7),
        ("cold-swim-monthly", "cold-water swimming", "sessions", 4, 30),
    ]
    for gname, act_name, metric, target, cadence in extra_goals:
        aid = act_id(act_name)
        if aid:
            con.execute(
                "INSERT OR IGNORE INTO goals"
                " (name, activity_id, metric, target, cadence_days) VALUES (?,?,?,?,?)",
                (gname, aid, metric, target, cadence),
            )
    print("goals         ✓")

    # ── 6. Historical sessions + feedback ─────────────────────────────────────
    # Skipped if any sessions already exist (idempotency guard).
    n_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    if n_sessions == 0:
        # (act_name, venue_name, days_ago, start_hour, dur_min, stars, note)
        history = [
            # Golf × 3 (within the 14-day goal cadence)
            ("golf",                "Golf Hodkovičky",                     1,  9, 210, 5, "Great round — 2 pars"),
            ("golf",                "Golf Hodkovičky",                     5,  9, 180, 4, "A bit windy"),
            ("golf",                "Golf Hodkovičky",                    10,  9, 210, 5, "Best round this year"),
            # Cycling × 4 (satisfies 2/week goal)
            ("cycling",             "Cycling start – Vltava riverbank",    2,  7,  90, 5, "Morning ride, felt strong"),
            ("cycling",             "Cycling start – Vltava riverbank",    6,  7, 120, 4, "Slight headwind"),
            ("cycling",             "Cycling start – Vltava riverbank",    9,  7,  90, 5, None),
            ("cycling",             "Cycling start – Vltava riverbank",   14,  7,  90, 4, None),
            # Cold-water swim × 2
            ("cold-water swimming", "Vltava cold swim – Císařský ostrov",  3,  8,  30, 5, "14 °C, invigorating"),
            ("cold-water swimming", "Vltava cold swim – Císařský ostrov", 12,  8,  30, 4, "A bit crowded"),
            # Hike × 1
            ("hike",                "Prokopské údolí trailhead",           7,  9, 180, 5, "Prokopák loop"),
            # Massage × 2 (recovery-weekly goal met)
            ("massage",             "Massage Studio – Centrum",            4, 14,  60, 4, "Deep tissue"),
            ("massage",             "Massage Studio – Centrum",           11, 14,  60, 5, "Full recovery"),
            # Swimming × 1
            ("swimming",            "Plavecký stadion Podolí",             8,  7,  60, 4, "2 km swim"),
            # Reading × 2 (lower-rated → pref stays below neutral)
            ("reading-spot",        "Café Slavia",                         2, 15,  90, 3, "Decent coffee, noisy"),
            ("reading-spot",        "Café Slavia",                        13, 15,  90, 4, "Good session"),
            # Sauna × 1
            ("sauna",               "Sauna Žluté lázně",                   6, 16,  60, 5, "Post-cycling recovery"),
        ]
        for act_name, venue_name, d, h, dur_min, stars, note in history:
            aid = act_id(act_name)
            vid = ven_id(venue_name)
            if not aid:
                continue
            clat, clon = ven_coords(vid)
            start = _ago(days=d, hour=h)
            end   = start + timedelta(minutes=dur_min)
            cur = con.execute(
                "INSERT INTO sessions"
                " (start_ts, end_ts, duration_s, centroid_lat, centroid_lon,"
                "  venue_id, activity_id, confidence, feedback_requested)"
                " VALUES (?,?,?,?,?,?,?,?,0)",
                (_ts(start), _ts(end), dur_min * 60, clat, clon, vid, aid, 0.92),
            )
            con.execute(
                "INSERT INTO feedback (session_id, stars, note, emotion, ts)"
                " VALUES (?,?,?,?,?)",
                (cur.lastrowid, stars, note, None, _ts(end) + 300),
            )

        # Open session — yesterday's golf, no feedback row yet (feedback_requested=1)
        # Use case B3: Telegram bot or dashboard ⭐ widget awaits rating.
        aid = act_id("golf")
        vid = ven_id("Golf Hodkovičky")
        clat, clon = ven_coords(vid)
        start = _ago(days=1, hour=10)
        end   = start + timedelta(hours=3)
        con.execute(
            "INSERT INTO sessions"
            " (start_ts, end_ts, duration_s, centroid_lat, centroid_lon,"
            "  venue_id, activity_id, confidence, feedback_requested)"
            " VALUES (?,?,?,?,?,?,?,?,1)",
            (_ts(start), _ts(end), 10800, clat, clon, vid, aid, 0.95),
        )
        print(f"sessions      ✓  ({len(history)} historical + 1 awaiting feedback)")
    else:
        print(f"sessions      – skipped ({n_sessions} already present)")

    # ── 7. GPS pings ──────────────────────────────────────────────────────────
    # Skipped if any pings already exist.
    n_pings = con.execute("SELECT COUNT(*) FROM pings").fetchone()[0]
    if n_pings == 0:
        # 5 home pings (anchor-suppressed: session_id=0 already set)
        # Use case B2: shows anchor suppression in /api/state pings list.
        home_lat, home_lon = 49.9621, 14.3838
        base_home = int(_ago(hours=4).timestamp())
        for i in range(5):
            con.execute(
                "INSERT INTO pings (ts, lat, lon, address, session_id) VALUES (?,?,?,?,?)",
                (
                    base_home + i * 600,
                    home_lat + random.uniform(-0.0001, 0.0001),
                    home_lon + random.uniform(-0.0001, 0.0001),
                    "Home (Neumannova/Faltysova)",
                    0,
                ),
            )

        # 15 pings at Podolí pool — session_id NULL (staypoint not yet detected)
        # Use case B1: POST /gps triggers _process_tail() which closes this staypoint,
        # creates a swimming session, and sends Telegram feedback request.
        pool_lat, pool_lon = 50.0592, 14.4128
        base_pool = int(_ago(hours=2).timestamp())
        for i in range(15):
            con.execute(
                "INSERT INTO pings"
                " (ts, lat, lon, address, altitude, weather_main, temp) VALUES (?,?,?,?,?,?,?)",
                (
                    base_pool + i * 300,   # 5-min intervals → 70 min dwell > 720 s threshold
                    pool_lat + random.uniform(-0.0002, 0.0002),
                    pool_lon + random.uniform(-0.0002, 0.0002),
                    "Podolí, Praha 4",
                    192.0,
                    "Clear",
                    22.5,
                ),
            )
        print("pings         ✓  (5 home/anchor + 15 staypoint-forming at Podolí)")
    else:
        print(f"pings         – skipped ({n_pings} already present)")

    # ── 8. Approved plan for today ────────────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    if not con.execute("SELECT id FROM plans WHERE date=?", (today,)).fetchone():
        payload = {
            "summary": (
                "Sunny June morning — golf is the clear top pick (Clear sky, 22 °C, "
                "daylight, goal cadence on track). Cycling and cold-water swim as "
                "strong alternatives."
            ),
            "picks": [
                {
                    "name": "golf",
                    "route": "NOW",
                    "utility": 0.87,
                    "reason": "Clear sky, 22 °C, daylight — ideal. Golf-improvement goal on track.",
                },
                {
                    "name": "cycling",
                    "route": "NEXT",
                    "utility": 0.74,
                    "reason": "Good weather window, slightly less urgent than golf today.",
                },
                {
                    "name": "cold-water swimming",
                    "route": "NEXT",
                    "utility": 0.62,
                    "reason": "Borderline water temp; go if morning energy is high.",
                },
            ],
            "ranked": [
                {"name": "golf",                "utility": 0.87, "route": "NOW"},
                {"name": "cycling",             "utility": 0.74, "route": "NEXT"},
                {"name": "cold-water swimming", "utility": 0.62, "route": "NEXT"},
                {"name": "hike",                "utility": 0.55, "route": "NEXT"},
                {"name": "massage",             "utility": 0.48, "route": "PARK"},
                {"name": "reading-spot",        "utility": 0.32, "route": "PARK"},
                {"name": "swimming",            "utility": 0.28, "route": "PARK"},
                {"name": "sauna",               "utility": 0.21, "route": "PARK"},
                {"name": "yoga",                "utility": 0.18, "route": "PARK"},
                {"name": "museum",              "utility": 0.09, "route": "PARK"},
            ],
            "conditions": {
                "season": "summer",
                "daytime": "daylight",
                "temp": 22.0,
                "weather": "Clear",
            },
        }
        con.execute(
            "INSERT INTO plans (date, horizon, status, payload, created_ts) VALUES (?,?,?,?,?)",
            (today, "daily", "approved", json.dumps(payload), _ts(_ago(hours=2))),
        )
        print("plan          ✓  (approved daily plan for today)")
    else:
        print("plan          – skipped (plan for today already exists)")

    # ── 9. Refresh goal progress ──────────────────────────────────────────────
    con.execute("""
        UPDATE goals SET progress = (
            SELECT COUNT(*) FROM sessions
            WHERE activity_id = goals.activity_id
              AND end_ts >= (CAST(strftime('%s','now') AS INTEGER) - cadence_days * 86400)
        )
    """)
    print("goal progress ✓")

    con.commit()
    con.close()

    print("""
Done. Open http://localhost:8000/dashboard to see the loaded data.

To trigger staypoint detection on the 15 Podolí pings (use case B1), send
one more GPS ping — the server will run _process_tail() and create a
swimming session + Telegram feedback request:

  curl -s -X POST http://localhost:8000/gps \\
    -H "Content-Type: application/json" \\
    -d '{"locations":[{"geometry":{"coordinates":[14.4128,50.0592]},"properties":{"timestamp":"2026-06-23T12:00:00Z","altitude":192}}]}'
""")


if __name__ == "__main__":
    main()
