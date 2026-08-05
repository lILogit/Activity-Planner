"""Telegram I/O: send plans with inline keyboards, parse webhook updates,
capture star feedback.
"""
import json
import re
import time

import httpx

from .config import settings
from .db import get_conn
from .scoring import update_posterior

API = "https://api.telegram.org/bot{token}/{method}"


async def _call(method: str, payload: dict) -> dict:
    if not settings.telegram_bot_token:
        return {}
    url = API.format(token=settings.telegram_bot_token, method=method)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload)
        return r.json()


async def send_message(text: str, keyboard: list[list[dict]] | None = None) -> dict:
    payload: dict = {"chat_id": settings.telegram_chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return await _call("sendMessage", payload)


async def trace(event: str) -> None:
    """Send a one-line pipeline trace to Telegram. Only fires when DEBUG_TRACE=true."""
    if not settings.debug_trace:
        return
    await send_message(f"[TRACE] {event}")


async def send_plan(plan_id: int, summary: str, picks: list[dict]) -> dict:
    await trace(f"📋 Plan #{plan_id}  {' · '.join(p['name'] + ' ' + p['route'] for p in picks)}")
    lines = [summary, ""]
    for p in picks:
        lines.append(f"• {p['name']} ({p['route']}) — {p['reason']}")
    keyboard = [[
        {"text": "✅ Approve", "callback_data": f"approve:{plan_id}"},
        {"text": "🔄 Reroll", "callback_data": f"reroll:{plan_id}"},
        {"text": "⏭️ Skip", "callback_data": f"skip:{plan_id}"},
    ]]
    return await send_message("\n".join(lines), keyboard)


async def request_feedback(session_id: int, activity_name: str, when: str) -> dict:
    with get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET feedback_requested = 1 WHERE id = ?", (session_id,)
        )
    await trace(f"⭐ Feedback requested  session #{session_id} {activity_name} {when}")
    text = (
        f"Detected {activity_name} {when}.\n"
        "Rate it 1–5 (just reply, e.g. `4 great pace #content`)."
    )
    return await send_message(text)


# ---------- unknown staypoint venue tagging ----------

async def request_unknown_venue(session_id: int, lat: float, lon: float, dwell_min: int) -> None:
    """Mark a session as pending a venue tag via /venue reply. State is now DB-backed."""
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET venue_pending = 1 WHERE id = ?", (session_id,))
    await trace(f"❓ Unknown session #{session_id} — asking user to identify venue")
    await send_message(
        f"📍 New staypoint: {dwell_min}min at ({lat:.4f}, {lon:.4f}) — no matching venue.\n"
        "Reply `/venue <name>, <activity>` to tag it.\n"
        "Example: `/venue Riegrovy sady, cycling`"
    )


async def _handle_enrich(text: str) -> None:
    """Handle /enrich <free text> Telegram command (Loop C via bot)."""
    from .llm import extract_events
    with get_conn() as conn:
        known = [r["name"] for r in conn.execute("SELECT name FROM activities").fetchall()]
    proposals = extract_events(text, known)
    if not proposals:
        await send_message("No events extracted — check ANTHROPIC_API_KEY or rephrase the text.")
        return

    import httpx as _httpx
    lines = []
    for p in proposals:
        venue_name = (p.get("venue_name") or "").strip()
        activity_name = (p.get("activity") or "").strip().lower()
        cluster = p.get("cluster", "SPORT")
        geocode_q = p.get("geocode_query", venue_name)
        lat = lon = None
        if settings.locationiq_api_key and geocode_q:
            try:
                async with _httpx.AsyncClient(timeout=10) as cli:
                    r = await cli.get(
                        "https://us1.locationiq.com/v1/search",
                        params={"key": settings.locationiq_api_key, "q": geocode_q, "format": "json", "limit": 1},
                    )
                    r.raise_for_status()
                    hits = r.json()
                    if hits:
                        lat, lon = float(hits[0]["lat"]), float(hits[0]["lon"])
            except Exception:
                pass
        if not activity_name:
            continue
        with get_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO activities (name, cluster) VALUES (?,?)", (activity_name, cluster))
            act_id = conn.execute("SELECT id FROM activities WHERE name=?", (activity_name,)).fetchone()["id"]
            if venue_name and lat is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO venues (name, lat, lon, radius_m, activity_id) VALUES (?,?,?,?,?)",
                    (venue_name, lat, lon, 300, act_id),
                )
            if p.get("event_date"):
                goal_name = f"{activity_name}-{p['event_date']}"
                conn.execute(
                    "INSERT OR IGNORE INTO goals (name, activity_id, metric, target, cadence_days) VALUES (?,?,?,?,?)",
                    (goal_name, act_id, "sessions", 1, 1),
                )
        geocoded = "✅" if lat is not None else "⚠️ no coords"
        lines.append(f"{geocoded}  {activity_name} @ {venue_name or '(no venue)'}")

    await send_message("Enriched:\n" + "\n".join(lines))


async def _handle_venue(text: str) -> None:
    """Handle /venue <name>, <activity> reply to tag an unknown staypoint.

    Reads the most recent session with venue_pending=1 from the DB (the lat/lon
    are already on the session row), then updates the venue name/activity and clears
    the flag. This removes the race condition from the old global _pending_venue.
    """
    parts = [p.strip() for p in text.split(",", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        await send_message("Format: `/venue <venue name>, <activity>`\nExample: `/venue Riegrovy sady, cycling`")
        return
    venue_name, activity_name = parts[0], parts[1].lower()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, centroid_lat, centroid_lon FROM sessions WHERE venue_pending = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            await send_message("No pending staypoint to tag right now.")
            return
        session_id, lat, lon = row["id"], row["centroid_lat"], row["centroid_lon"]

        conn.execute("INSERT OR IGNORE INTO activities (name, cluster) VALUES (?,?)", (activity_name, "SPORT"))
        act_id = conn.execute("SELECT id FROM activities WHERE name=?", (activity_name,)).fetchone()["id"]

        # Check if a venue already exists at this location with the same name (from auto-create)
        existing = conn.execute("SELECT id FROM venues WHERE name=? AND lat=? AND lon=?", (venue_name, lat, lon)).fetchone()
        if existing:
            venue_id = existing["id"]
            conn.execute("UPDATE venues SET activity_id = ? WHERE id = ?", (act_id, venue_id))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO venues (name, lat, lon, radius_m, activity_id) VALUES (?,?,?,?,?)",
                (venue_name, lat, lon, 200, act_id),
            )
            venue_id = conn.execute("SELECT id FROM venues WHERE name=?", (venue_name,)).fetchone()["id"]

        conn.execute(
            "UPDATE sessions SET venue_id=?, activity_id=?, venue_pending = 0 WHERE id=?",
            (venue_id, act_id, session_id),
        )

    await trace(f"📍 Session #{session_id} tagged → {activity_name} @ {venue_name}")
    await request_feedback(session_id, activity_name, "earlier today")


# ---------- inbound webhook handling ----------

_STARS_RE = re.compile(r"\b([1-5])\b")
_EMOTION_RE = re.compile(r"#(\w+)")


def parse_feedback(text: str) -> tuple[int | None, str, str | None]:
    stars = None
    m = _STARS_RE.search(text)
    if m:
        stars = int(m.group(1))
    emotion = None
    e = _EMOTION_RE.search(text)
    if e:
        emotion = e.group(1)
    note = _EMOTION_RE.sub("", text)
    if m:
        note = note.replace(m.group(1), "", 1)
    return stars, note.strip(), emotion


async def handle_update(update: dict) -> None:
    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
    elif "message" in update and "text" in update["message"]:
        await _handle_text(update["message"]["text"])


async def _handle_callback(cq: dict) -> None:
    data = cq.get("data", "")
    if ":" not in data:
        return
    action, plan_id = data.split(":", 1)
    await trace(f"📲 Callback  {action} plan #{plan_id}")
    status = {"approve": "approved", "skip": "rejected", "reroll": "rejected"}.get(action)
    if status:
        with get_conn() as conn:
            conn.execute("UPDATE plans SET status = ? WHERE id = ?", (status, plan_id))
    await _call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": f"{action} ✓"})
    # Send a persistent confirmation message (answerCallbackQuery only shows a fleeting popup)
    _confirmations = {
        "approve": "✅ Plan approved. Enjoy your day!",
        "skip":    "⏭️ Plan skipped. See you tomorrow.",
        "reroll":  "🔄 Generating a new plan…",
    }
    if action in _confirmations:
        await send_message(_confirmations[action])
    if action == "reroll":
        from .planner import build_and_send_plan  # late import to avoid cycle
        await build_and_send_plan(horizon="daily")


async def _handle_text(text: str) -> None:
    t = text.strip()
    if t.lower().startswith("/enrich "):
        await _handle_enrich(t[8:].strip())
        return
    if t.lower().startswith("/venue "):
        await _handle_venue(t[7:].strip())
        return
    stars, note, emotion = parse_feedback(t)
    if stars is None:
        await send_message("Send a rating 1–5 to log feedback, or use /enrich <text> to add an activity.")
        return
    with get_conn() as conn:
        row = conn.execute(
            """SELECT s.id, s.activity_id, a.alpha, a.beta
               FROM sessions s JOIN activities a ON a.id = s.activity_id
               WHERE s.feedback_requested = 1
                 AND s.id NOT IN (SELECT session_id FROM feedback)
               ORDER BY s.end_ts DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            await send_message("No session is awaiting feedback right now.")
            return
        conn.execute(
            "INSERT INTO feedback (session_id, stars, note, emotion, ts) VALUES (?,?,?,?,?)",
            (row["id"], stars, note, emotion, int(time.time())),
        )
        new_a, new_b = update_posterior(row["alpha"], row["beta"], stars)
        conn.execute(
            "UPDATE activities SET alpha = ?, beta = ? WHERE id = ?",
            (new_a, new_b, row["activity_id"]),
        )
        act_name = conn.execute(
            "SELECT name FROM activities WHERE id = ?", (row["activity_id"],)
        ).fetchone()["name"]
        new_pref = round(new_a / (new_a + new_b) * 100)
    star_str = "⭐" * stars
    emotion_str = f" #{emotion}" if emotion else ""
    note_str = f' — "{note}"' if note else ""
    await trace(f"⭐ Feedback logged  {act_name} {star_str}{emotion_str} → pref {new_pref}%")
    await send_message(
        f"{star_str} {act_name} logged{note_str}{emotion_str}.\n"
        f"Preference updated to {new_pref}%."
    )
