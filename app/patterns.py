"""Deterministic route/pattern detection over ordered sessions.

LLM-free, unit-testable. Reads sessions ordered by start_ts, builds consecutive
venue-pair transitions, and returns patterns that recur ≥ min_repeats.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import List

from .config import settings
from .db import get_conn
from .geo import haversine_m


@dataclass
class Pattern:
    """A repeated origin→destination route."""
    from_venue_id: int | None
    to_venue_id: int | None
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    count: int
    typical_hour: int  # modal hour (0-23)
    first_seen: int  # epoch seconds
    last_seen: int
    kind: str = "routine"  # routine | commute | trip


def detect_patterns(min_repeats: int = 3, gap_m: float = 300.0) -> List[dict]:
    """Detect repeated origin→destination routes from sessions with venue_id.

    Args:
        min_repeats: minimum times a venue pair must appear consecutively to be a pattern
        gap_m: max distance in meters for two venues to be considered the same location

    Returns:
        List of dicts, each representing a Pattern (serializable for JSON/DB).
    """
    with get_conn() as conn:
        sessions = conn.execute(
            """SELECT id, venue_id, centroid_lat, centroid_lon, start_ts
               FROM sessions WHERE venue_id IS NOT NULL
               ORDER BY start_ts ASC"""
        ).fetchall()

    if len(sessions) < 2:
        return []

    # Build consecutive venue-pair transitions
    transitions = []
    for i in range(len(sessions) - 1):
        s1, s2 = sessions[i], sessions[i + 1]
        # Merge venues within gap_m (e.g., auto-created vs named venue at same place)
        from_venue_id = s1["venue_id"]
        to_venue_id = s2["venue_id"]
        from_lat, from_lon = s1["centroid_lat"], s1["centroid_lon"]
        to_lat, to_lon = s2["centroid_lat"], s2["centroid_lon"]

        # Skip if origin and destination are effectively the same place
        if haversine_m(from_lat, from_lon, to_lat, to_lon) < gap_m:
            continue

        transitions.append(
            {
                "from_venue_id": from_venue_id,
                "to_venue_id": to_venue_id,
                "from_lat": from_lat,
                "from_lon": from_lon,
                "to_lat": to_lat,
                "to_lon": to_lon,
                "hour": datetime.fromtimestamp(s2["start_ts"]).hour,
                "ts": s2["start_ts"],
            }
        )

    if not transitions:
        return []

    # Count occurrences of each (from_venue_id, to_venue_id) pair
    pair_counter = Counter()
    first_seen = {}
    last_seen = {}
    hour_counter = defaultdict(Counter)  # (from, to) -> {hour: count}

    for t in transitions:
        key = (t["from_venue_id"], t["to_venue_id"])
        pair_counter[key] += 1
        if key not in first_seen:
            first_seen[key] = t["ts"]
        last_seen[key] = t["ts"]
        hour_counter[key][t["hour"]] += 1

    # Filter to pairs that appear at least min_repeats times
    patterns = []
    for (from_vid, to_vid), count in pair_counter.items():
        if count < min_repeats:
            continue

        # Get the most common hour for this pair
        typical_hour = hour_counter[(from_vid, to_vid)].most_common(1)[0][0]

        # Find the lat/lon for the first occurrence of this pair
        first_t = next(t for t in transitions if t["from_venue_id"] == from_vid and t["to_venue_id"] == to_vid)

        patterns.append(
            {
                "from_venue_id": from_vid,
                "to_venue_id": to_vid,
                "from_lat": first_t["from_lat"],
                "from_lon": first_t["from_lon"],
                "to_lat": first_t["to_lat"],
                "to_lon": first_t["to_lon"],
                "count": count,
                "typical_hour": typical_hour,
                "first_seen": first_seen[(from_vid, to_vid)],
                "last_seen": last_seen[(from_vid, to_vid)],
                "kind": "routine",
            }
        )

    return patterns


def upsert_patterns(patterns: List[dict]) -> int:
    """Write patterns to the patterns table, updating counts for existing ones.

    Returns the number of patterns upserted.
    """
    if not patterns:
        return 0

    with get_conn() as conn:
        upserted = 0
        for p in patterns:
            # Check if a pattern with the same from/to already exists
            existing = conn.execute(
                """SELECT id FROM patterns
                   WHERE from_venue_id = ? AND to_venue_id = ?""",
                (p["from_venue_id"], p["to_venue_id"]),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE patterns
                       SET count = ?, typical_hour = ?, first_seen = ?, last_seen = ?
                       WHERE id = ?""",
                    (
                        p["count"],
                        p["typical_hour"],
                        p["first_seen"],
                        p["last_seen"],
                        existing["id"],
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO patterns
                       (from_venue_id, to_venue_id, from_lat, from_lon, to_lat, to_lon,
                        count, typical_hour, first_seen, last_seen, kind)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        p["from_venue_id"],
                        p["to_venue_id"],
                        p["from_lat"],
                        p["from_lon"],
                        p["to_lat"],
                        p["to_lon"],
                        p["count"],
                        p["typical_hour"],
                        p["first_seen"],
                        p["last_seen"],
                        p["kind"],
                    ),
                )
            upserted += 1

        # Clean up old patterns that haven't been seen in a while (optional: >90 days stale)
        # conn.execute("DELETE FROM patterns WHERE last_seen < ?", (int(time.time()) - 90*86400,))

    return upserted
