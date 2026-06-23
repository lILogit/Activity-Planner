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
        lines.append(f"• {p['name']} — {p['reason']}")
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
    stars, note, emotion = parse_feedback(text)
    if stars is None:
        await send_message("Send a rating 1–5 to log feedback, or wait for the next plan.")
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
