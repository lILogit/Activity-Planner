"""Direct Anthropic API calls — the only places the LLM earns its keep.

Both functions fall back to deterministic heuristics when no key is set, so the
core loop runs without the model.
"""
import json
import re

from .config import settings

_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)

try:
    from anthropic import Anthropic
    _client = Anthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None
except Exception:  # SDK not installed yet
    _client = None


def classify_staypoint(staypoint: dict, candidate_venues: list[dict], planned: list[str]) -> dict:
    """Return {activity_id, venue_id, confidence}. Heuristic fallback if no LLM."""
    # Heuristic: nearest matching venue, boosted if its activity was planned today.
    best = None
    if candidate_venues:
        best = candidate_venues[0]  # caller pre-sorts by distance
    activity_id = best["activity_id"] if best else None
    venue_id = best["id"] if best else None
    conf = 0.5 if best else 0.2
    if best and best.get("activity_name") in planned:
        conf = 0.85

    if _client is None:
        return {"activity_id": activity_id, "venue_id": venue_id, "confidence": conf}

    prompt = (
        "Classify what activity a person did during a GPS staypoint. "
        "Respond ONLY with JSON: {\"activity_id\": int|null, \"confidence\": float}. No prose.\n\n"
        f"Staypoint: {json.dumps(staypoint)}\n"
        f"Candidate venues (sorted by distance): {json.dumps(candidate_venues)}\n"
        f"Activities planned for today: {json.dumps(planned)}\n"
        "Weight time-of-day, dwell length vs the venue's typical duration, and the planned prior."
    )
    try:
        resp = _client.messages.create(
            model=settings.model_fast,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = _JSON_RE.search(text)
        out = json.loads(m.group()) if m else {}
        return {
            "activity_id": out.get("activity_id", activity_id),
            "venue_id": venue_id,
            "confidence": float(out.get("confidence", conf)),
        }
    except Exception:
        return {"activity_id": activity_id, "venue_id": venue_id, "confidence": conf}


_JSON_ARRAY_RE = re.compile(r"\[.*?\]", re.DOTALL)

KNOWN_CLUSTERS = (
    "SPORT", "CULTURE", "RELAX", "RECOVERY", "STUDY",
    "WORK", "LIFE ADMIN", "FAMILY / CARE", "HEALTH / WELLNESS",
)


def extract_events(text: str, known_activities: list[str]) -> list[dict]:
    """Parse free text into a list of enrichment proposals.

    Each item: {venue_name, geocode_query, activity, cluster, event_date|null, notes}.
    Heuristic fallback returns [] so callers handle the no-LLM case gracefully.
    """
    if _client is None:
        return []
    prompt = (
        "Extract leisure activity events from the text below. "
        "Return a JSON ARRAY (no prose, no markdown) where each element has:\n"
        '  "venue_name": string — proper name of the place\n'
        '  "geocode_query": string — best search query to geocode it (include city/country)\n'
        f'  "activity": string — one of {json.dumps(known_activities)} or a new lowercase name\n'
        f'  "cluster": one of {json.dumps(KNOWN_CLUSTERS)}\n'
        '  "event_date": "YYYY-MM-DD" or null\n'
        '  "notes": string — brief human note\n\n'
        f"Text: {text}"
    )
    try:
        resp = _client.messages.create(
            model=settings.model_smart,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = _JSON_ARRAY_RE.search(raw)
        return json.loads(m.group()) if m else []
    except Exception:
        return []


def plan_rationale(picks: list[dict], conditions: dict) -> str:
    """One human sentence summarizing the plan. Falls back to joined reasons."""
    fallback = " ".join(p.get("reason", "") for p in picks).strip() or "No suitable activity today."
    if _client is None or not picks:
        return fallback
    prompt = (
        "Write ONE short, friendly sentence (max 30 words) summarizing today's leisure plan "
        "for the user, given the picks and conditions. No markdown.\n\n"
        f"Conditions: {json.dumps(conditions)}\n"
        f"Picks: {json.dumps(picks)}"
    )
    try:
        resp = _client.messages.create(
            model=settings.model_smart,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip() or fallback
    except Exception:
        return fallback
