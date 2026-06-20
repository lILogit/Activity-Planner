"""Planner: turn current conditions + inventory into a ranked, routed plan."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings
from .db import get_conn
from .llm import plan_rationale
from .scoring import Conditions, select, utility
from . import telegram


def _season(month: int) -> str:
    return {12: "winter", 1: "winter", 2: "winter",
            3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer",
            9: "autumn", 10: "autumn", 11: "autumn"}[month]


def gather_conditions() -> Conditions:
    """Derive season/daytime from now; weather/temp from the latest ping."""
    now = datetime.now(ZoneInfo(settings.tz))
    try:
        from astral import LocationInfo
        from astral.sun import sun as _astral_sun
        _city = LocationInfo("Prague", "Czech Republic", settings.tz, 50.08, 14.44)
        _s = _astral_sun(_city.observer, date=now.date(), tzinfo=ZoneInfo(settings.tz))
        daytime = _s["sunrise"] <= now <= _s["sunset"]
    except Exception:
        daytime = 7 <= now.hour <= 20  # fallback if astral not installed
    weather_main = temp = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT weather_main, temp FROM pings ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            weather_main, temp = row["weather_main"], row["temp"]
    return Conditions(
        season=_season(now.month), daytime=daytime, temp=temp, weather_main=weather_main
    )


def _goal_gap(activity_id: int, conn) -> float:
    """Fraction of the goal still unmet for this activity (0 = met, 1 = nothing done)."""
    g = conn.execute(
        "SELECT target, cadence_days FROM goals WHERE activity_id = ?", (activity_id,)
    ).fetchone()
    if not g:
        return 0.0
    since = int(time.time()) - g["cadence_days"] * 86400
    done = conn.execute(
        "SELECT COUNT(*) n FROM sessions WHERE activity_id = ? AND end_ts >= ?",
        (activity_id, since),
    ).fetchone()["n"]
    return max(0.0, 1.0 - done / g["target"]) if g["target"] else 0.0


def build_plan(horizon: str = "daily", k: int = 1) -> dict:
    c = gather_conditions()
    with get_conn() as conn:
        activities = [dict(r) for r in conn.execute("SELECT * FROM activities").fetchall()]
        rules = [dict(r) for r in conn.execute("SELECT * FROM rules").fetchall()]
        n_total = conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"]
        counts = {
            r["activity_id"]: r["n"]
            for r in conn.execute(
                "SELECT activity_id, COUNT(*) n FROM sessions GROUP BY activity_id"
            ).fetchall()
        }
        scored = [
            utility(
                a, c, rules,
                n_a=counts.get(a["id"], 0),
                n_total=n_total,
                goal_gap=_goal_gap(a["id"], conn),
            )
            for a in activities
        ]
        picks = select(scored, k=max(1, k))
        cond_dict = {
            "season": c.season, "daytime": c.daytime,
            "temp": c.temp, "weather": c.weather_main,
        }
        pick_dicts = [vars(p) for p in picks]
        summary = plan_rationale(pick_dicts, cond_dict)

        payload = {
            "conditions": cond_dict,
            "summary": summary,
            "picks": pick_dicts,
            "ranked": [vars(s) for s in sorted(scored, key=lambda s: s.utility, reverse=True)],
        }
        cur = conn.execute(
            "INSERT INTO plans (date, horizon, status, payload, created_ts) VALUES (?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d"), horizon, "proposed",
             json.dumps(payload), int(time.time())),
        )
        plan_id = cur.lastrowid
    return {"plan_id": plan_id, **payload}


async def build_and_send_plan(horizon: str = "daily") -> dict:
    k = 3 if horizon == "weekly" else 1
    plan = build_plan(horizon=horizon, k=k)
    await telegram.send_plan(plan["plan_id"], plan["summary"], plan["picks"])
    return plan
