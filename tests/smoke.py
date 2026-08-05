"""Keyless smoke test (CLAUDE.md): no network, no API keys.

Asserts:
  1. sunny + spring + daylight  -> golf scores NOW and is unblocked
  2. rain                       -> golf blocked (U=0, PARK)
  3. >12-min home dwell         -> exactly one closed staypoint
  4. update_posterior 5,5,4     -> preference > 0.7
"""
import os
import sys
import tempfile

# Force a throwaway datastore before any app import (settings reads env at import).
with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _fh:
    _DB = _fh.name
os.environ["DB_PATH"] = _DB

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_conn, init_db                       # noqa: E402
from app.geo import segment_staypoints                      # noqa: E402
from app.scoring import Conditions, update_posterior, utility  # noqa: E402

init_db(seed=True)

FAIL = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


with get_conn() as conn:
    golf = dict(conn.execute("SELECT * FROM activities WHERE name='golf'").fetchone())
    rules = [dict(r) for r in conn.execute("SELECT * FROM rules").fetchall()]

# 1 + 2: scoring & routing
sunny = Conditions(season="spring", daytime=True, temp=20.0, weather_main="Clear")
s = utility(golf, sunny, rules, n_a=0, n_total=0, goal_gap=0.0)
check("sunny spring daylight -> golf NOW",
      (not s.blocked) and s.route == "NOW",
      f"blocked={s.blocked} route={s.route} utility={s.utility} fit={s.fit}")

check("sunny golf utility > 0", s.utility > 0, f"utility={s.utility}")

rainy = Conditions(season="spring", daytime=True, temp=20.0, weather_main="Rain")
r = utility(golf, rainy, rules, n_a=0, n_total=0, goal_gap=0.0)
check("rain blocks golf (U=0)", r.blocked and r.utility == 0.0,
      f"blocked={r.blocked} utility={r.utility}")
check("rain routes golf to PARK", r.route == "PARK", f"route={r.route}")

# 3: a >12-min home dwell yields exactly one staypoint
# Home (Neumannova/Faltysova) lat=49.9621 lon=14.3838; min dwell default 720s.
base_ts = 1_700_000_000
t = 1.0e-4  # ~11 m lat jitter, well inside the 150 m radius
points = []
for i in range(5):                       # 5 pings, 4 min apart -> 16 min dwell
    points.append({
        "id": i + 1, "ts": base_ts + i * 240,
        "lat": 49.9621 + (t if i % 2 == 0 else -t), "lon": 14.3838,
    })
# a final ping far away closes the cluster
points.append({"id": 6, "ts": base_ts + 5 * 240, "lat": 49.9700, "lon": 14.3900})
res = segment_staypoints(points, radius_m=150.0, min_dwell_s=720.0)
check(">12-min home dwell -> exactly one staypoint", len(res.closed) == 1,
      f"closed={len(res.closed)}")

# 4: update_posterior after 5,5,4 pushes preference > 0.7
a, b = 1.0, 1.0
for stars in (5, 5, 4):
    a, b = update_posterior(a, b, stars)
pref = a / (a + b)
check("posterior 5,5,4 -> pref > 0.7", pref > 0.7, f"a={a} b={b} pref={pref:.3f}")

# 5: pattern detection from repeated A→B venue transitions
from app.patterns import detect_patterns, upsert_patterns
with get_conn() as conn:
    # Insert two venues
    conn.execute("INSERT INTO venues (name, lat, lon, radius_m, activity_id) VALUES (?,?,?,?,?)",
                  ("Home A", 49.9621, 14.3838, 200, 1))
    conn.execute("INSERT INTO venues (name, lat, lon, radius_m, activity_id) VALUES (?,?,?,?,?)",
                  ("Work B", 50.0847, 14.4208, 200, 1))
    home_venue = conn.execute("SELECT id FROM venues WHERE name='Home A'").fetchone()["id"]
    work_venue = conn.execute("SELECT id FROM venues WHERE name='Work B'").fetchone()["id"]
    # Create 6 sessions: Home → Work → Home → Work → Home → Work (four A→B transitions)
    for i, (from_v, to_v) in enumerate([(home_venue, work_venue), (work_venue, home_venue),
                                         (home_venue, work_venue), (work_venue, home_venue),
                                         (home_venue, work_venue), (work_venue, home_venue)]):
        start = base_ts + i * 86400
        conn.execute(
            "INSERT INTO sessions (start_ts, end_ts, duration_s, centroid_lat, centroid_lon, venue_id, activity_id, confidence) VALUES (?,?,?,?,?,?,?,?)",
            (start, start + 3600, 3600, 49.9 if from_v == home_venue else 50.0, 14.4 if from_v == home_venue else 14.5,
             from_v, 1, 0.8)
        )
patterns = detect_patterns(min_repeats=3)
check("pattern detection finds A→B route with count>=3",
      any(p["count"] >= 3 and p["from_venue_id"] == home_venue and p["to_venue_id"] == work_venue
          for p in patterns),
      f"patterns={len(patterns)}")

print()
if FAIL:
    print(f"{len(FAIL)} check(s) FAILED: {', '.join(FAIL)}")
    sys.exit(1)
print("ALL CHECKS PASSED")
