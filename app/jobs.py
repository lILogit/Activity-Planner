"""APScheduler jobs — the three loops, in-process."""
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .db import get_conn
from .planner import build_and_send_plan, gather_conditions
from .scoring import Conditions, beta_mean, fit, rule_effect
from . import telegram


async def morning_plan() -> None:
    await build_and_send_plan(horizon="daily")


async def weekly_plan() -> None:
    await build_and_send_plan(horizon="weekly")


async def kairos_window() -> None:
    """Anti-missed-opportunity scan: rare favorable window x high pref x staleness."""
    c: Conditions = gather_conditions()
    with get_conn() as conn:
        activities = [dict(r) for r in conn.execute(
            "SELECT * FROM activities WHERE active = 1").fetchall()]
        rules = [dict(r) for r in conn.execute("SELECT * FROM rules").fetchall()]
        for a in activities:
            blocked, factor = rule_effect(a["id"], rules, c)
            if blocked:
                continue
            f = fit(a, c)
            pref = beta_mean(a["alpha"], a["beta"])
            last = conn.execute(
                "SELECT MAX(end_ts) t FROM sessions WHERE activity_id = ?", (a["id"],)
            ).fetchone()["t"]
            days_since = (int(time.time()) - last) / 86400 if last else 999
            score = f * factor * (1 + pref)
            if (score >= settings.kairos_min_score
                    and days_since >= settings.kairos_min_days_since):
                await telegram.send_message(
                    f"⏳ Window open for {a['name']} "
                    f"({c.weather_main or 'mild'}, {c.temp}°C). "
                    f"You haven't done it in {int(days_since)} days — go?"
                )


async def enrichment() -> None:
    """Loop C stub: pull sources, propose new activities/venues for approval.

    Wire web_search / channel scrape + llm extraction here, then send a
    Telegram approval prompt before inserting rows.
    """
    # TODO: implement source fetch + extraction; left as a stub on purpose.
    return None


async def detect_patterns_job() -> None:
    """Daily route pattern detection job."""
    from .patterns import detect_patterns, upsert_patterns
    patterns = detect_patterns(min_repeats=3)
    if patterns:
        upserted = upsert_patterns(patterns)
        await telegram.trace(f"🔁 Patterns updated: {upserted} routes")


def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.tz)
    sched.add_job(morning_plan, CronTrigger(hour=6, minute=30), id="morning_plan")
    sched.add_job(weekly_plan, CronTrigger(day_of_week="sun", hour=18), id="weekly_plan")
    sched.add_job(kairos_window, CronTrigger(minute=0), id="kairos_window")  # hourly
    sched.add_job(enrichment, CronTrigger(day_of_week="mon", hour=7), id="enrichment")
    sched.add_job(detect_patterns_job, CronTrigger(hour=3, minute=17), id="detect_patterns")
    return sched
