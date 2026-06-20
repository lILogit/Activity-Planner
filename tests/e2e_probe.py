"""Live end-to-end probe against the running uvicorn server (keyless).

Walks the three loops over HTTP + in-process planning:
  /gps near home (anchor)        -> anchor suppression, no session
  + insert a golf venue, /gps     -> staypoint -> session (golf), feedback requested
  /tg star feedback              -> feedback row + Beta posterior updated
  planner.build_plan (in-process) -> plan row written
  /tg approve callback           -> plan status -> approved
"""
import sys

import httpx

BASE = "http://127.0.0.1:8000"
fails = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def overland(lat, lon, ts_iso, alt=None, wifi=None):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"timestamp": ts_iso, "altitude": alt, "wifi": wifi},
    }


def post_locations(points):
    r = httpx.post(f"{BASE}/gps", json={"locations": points}, timeout=30)
    r.raise_for_status()
    return r.json()


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _text_update(text: str) -> dict:
    return {"message": {"text": text, "chat": {"id": 1}, "from": {"id": 1}}}


def _callback_update(plan_id: int, action: str) -> dict:
    return {"callback_query": {"id": "x", "data": f"{action}:{plan_id}",
                               "from": {"id": 1}}}


# ---------- DB helpers (same file the server uses) ----------
from app.db import get_conn
from app.planner import build_plan


def counts():
    with get_conn() as c:
        return {
            "pings": c.execute("SELECT COUNT(*) n FROM pings").fetchone()["n"],
            "sessions": c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"],
            "anchor_pings": c.execute(
                "SELECT COUNT(*) n FROM pings WHERE session_id = 0"
            ).fetchone()["n"],
            "feedback": c.execute("SELECT COUNT(*) n FROM feedback").fetchone()["n"],
            "plans": c.execute("SELECT COUNT(*) n FROM plans").fetchone()["n"],
        }


print("== 1. anchor suppression: dwell at home ==")
HOME = (49.9621, 14.3838)
t0 = 1_700_000_000
dwell = [
    overland(HOME[0] + 1e-5 * (i % 2), HOME[1], _iso(t0 + i * 240))
    for i in range(5)  # 16 min
]
# far point closes the cluster
dwell.append(overland(49.9700, 14.3900, _iso(t0 + 5 * 240)))
res = post_locations(dwell)
check("/gps returns ok", res.get("result") == "ok", res)
c = counts()
check("home dwell suppressed (no session)", c["sessions"] == 0, c)
check("home pings marked sentinel session_id=0", c["anchor_pings"] == 5, c)

print("\n== 2. staypoint -> session: dwell at a golf venue ==")
with get_conn() as conn:
    conn.execute(
        "INSERT INTO venues (name, lat, lon, radius_m, activity_id, is_anchor) "
        "VALUES ('Hostivař Golf', 50.0610, 14.5360, 300, "
        "(SELECT id FROM activities WHERE name='golf'), 0)"
    )
GOLF = (50.0610, 14.5360)
t1 = t0 + 100000
golf_pts = [
    overland(GOLF[0] + 1e-4 * (i % 2), GOLF[1], _iso(t1 + i * 300))
    for i in range(4)  # 15 min
]
golf_pts.append(overland(50.0700, 14.5400, _iso(t1 + 4 * 300)))
post_locations(golf_pts)
c = counts()
check("golf dwell created a session", c["sessions"] == 1, c)
with get_conn() as conn:
    sess = dict(conn.execute(
        "SELECT activity_id, feedback_requested, confidence FROM sessions "
        "ORDER BY id DESC LIMIT 1").fetchone())
    act_id = sess["activity_id"]
check("session classified as golf",
      act_id is not None, sess)
check("feedback requested for session",
      sess["feedback_requested"] == 1, sess)

print("\n== 3. /tg star feedback updates the posterior ==")
with get_conn() as conn:
    pref_before = dict(conn.execute(
        "SELECT alpha, beta FROM activities WHERE name='golf'").fetchone())
before_pref = pref_before["alpha"] / (pref_before["alpha"] + pref_before["beta"])
httpx.post(f"{BASE}/tg", json=_text_update("5 fantastic round #stoked"), timeout=15)
c = counts()
check("feedback row written", c["feedback"] == 1, c)
with get_conn() as conn:
    pref_after = dict(conn.execute(
        "SELECT alpha, beta FROM activities WHERE name='golf'").fetchone())
after_pref = pref_after["alpha"] / (pref_after["alpha"] + pref_after["beta"])
check("golf preference moved up after 5 stars",
      after_pref > before_pref, f"{before_pref:.3f} -> {after_pref:.3f}")

print("\n== 4. planner.build_plan (loop A, keyless) ==")
plan = build_plan(horizon="daily", k=1)
check("plan written", "plan_id" in plan and plan["plan_id"] > 0, plan.get("plan_id"))
print("   summary:", plan["summary"])
for p in plan["picks"]:
    print(f"   pick: {p['name']} [{p['route']}] util={p['utility']} — {p['reason']}")

print("\n== 5. /tg approve callback flips plan status ==")
httpx.post(f"{BASE}/tg", json=_callback_update(plan["plan_id"], "approve"), timeout=15)
with get_conn() as conn:
    status = conn.execute(
        "SELECT status FROM plans WHERE id = ?", (plan["plan_id"],)).fetchone()["status"]
check("plan approved via callback", status == "approved", f"status={status}")

print("\n== final /admin ==")
adm = httpx.get(f"{BASE}/admin", timeout=15).json()
print(f"   pings={adm['pings']} sessions={adm['sessions']}")
for a in adm["activities"]:
    if a["name"] == "golf":
        print(f"   golf pref now {a['pref']}")

print()
if fails:
    print(f"{len(fails)} FAILED: {', '.join(fails)}")
    sys.exit(1)
print("ALL E2E CHECKS PASSED")
