"""Geometry: Haversine distance and online staypoint segmentation.

A staypoint is a run of consecutive pings whose centroid stays inside
`radius_m` for at least `min_dwell_s`. We recompute over the unassigned tail
on every new ping, which is robust across process restarts (state lives in
the DB, not in memory).
"""
from dataclasses import dataclass, field
from math import atan2, cos, radians, sin, sqrt

EARTH_R_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    )
    return EARTH_R_M * 2 * atan2(sqrt(a), sqrt(1 - a))


@dataclass
class Staypoint:
    point_ids: list[int]
    start_ts: int
    end_ts: int
    centroid_lat: float
    centroid_lon: float

    @property
    def dwell_s(self) -> float:
        return float(self.end_ts - self.start_ts)


@dataclass
class SegmentResult:
    closed: list[Staypoint] = field(default_factory=list)
    open_tail: list[int] = field(default_factory=list)  # ping ids still accumulating


def segment_staypoints(
    points: list[dict],
    radius_m: float,
    min_dwell_s: float,
) -> SegmentResult:
    """`points`: dicts with keys id, ts (epoch s), lat, lon — sorted ascending by ts.

    Returns closed staypoints plus the still-open trailing cluster. Movement
    (a point outside the current centroid radius) closes the active cluster;
    clusters shorter than `min_dwell_s` are treated as transit and dropped.
    """
    res = SegmentResult()
    if not points:
        return res

    cluster: list[dict] = []
    c_lat = c_lon = 0.0

    def centroid(c: list[dict]) -> tuple[float, float]:
        return (
            sum(p["lat"] for p in c) / len(c),
            sum(p["lon"] for p in c) / len(c),
        )

    for p in points:
        if not cluster:
            cluster = [p]
            c_lat, c_lon = p["lat"], p["lon"]
            continue

        if haversine_m(c_lat, c_lon, p["lat"], p["lon"]) <= radius_m:
            cluster.append(p)
            c_lat, c_lon = centroid(cluster)
        else:
            # movement: close the current cluster
            dwell = cluster[-1]["ts"] - cluster[0]["ts"]
            if dwell >= min_dwell_s:
                res.closed.append(
                    Staypoint(
                        point_ids=[c["id"] for c in cluster],
                        start_ts=cluster[0]["ts"],
                        end_ts=cluster[-1]["ts"],
                        centroid_lat=c_lat,
                        centroid_lon=c_lon,
                    )
                )
            cluster = [p]
            c_lat, c_lon = p["lat"], p["lon"]

    # the trailing cluster stays open (may still be growing)
    res.open_tail = [c["id"] for c in cluster]
    return res
