"""Utility scoring and learning.

U(a|c,t) = w_pref·Pref + w_fit·Fit + w_prog·Prog + w_expl·Explore, scaled by
rule activation, with BLOCK rules excluding a candidate outright.
"""
import random
from dataclasses import dataclass
from math import log, sqrt

from .config import settings

CLUSTERS = (
    "SPORT", "CULTURE", "RELAX", "RECOVERY", "STUDY",
    "WORK", "LIFE ADMIN", "FAMILY / CARE", "HEALTH / WELLNESS",
    # legacy — kept so existing data isn't orphaned
    "TRIP", "SOCIAL",
)
ROUTES = ("NOW", "NEXT", "PARK", "ARCHIVE")


# ---------- preference posterior (Beta over normalized stars) ----------

def beta_mean(alpha: float, beta: float) -> float:
    return alpha / (alpha + beta)


def thompson_sample(alpha: float, beta: float) -> float:
    return random.betavariate(alpha, beta)


def update_posterior(alpha: float, beta: float, stars: int) -> tuple[float, float]:
    """Stars 1..5 -> pseudo-success in [0,1]; Beta-Bernoulli update."""
    s = max(1, min(5, stars)) / 5.0
    return alpha + s, beta + (1.0 - s)


# ---------- exploration bonus ----------

def explore_ucb(n_a: int, n_total: int) -> float:
    if n_a <= 0:
        return 1.0  # never tried -> maximal curiosity
    if n_total <= 0:
        return 0.0
    return sqrt(2.0 * log(n_total) / n_a)


# ---------- condition fit against an activity's hard-ish gates ----------

@dataclass
class Conditions:
    season: str            # spring|summer|autumn|winter
    daytime: bool          # True if daylight
    temp: float | None
    weather_main: str | None   # OpenWeather 'main', e.g. Clear, Rain, Snow


def fit(activity: dict, c: Conditions) -> float:
    """Soft match in [0,1] from season / daylight / temperature gates."""
    score = 1.0
    if c.season not in activity["season_mask"].split(","):
        score *= 0.15
    if activity["daylight_required"] and not c.daytime:
        score *= 0.05
    if c.temp is not None and activity["t_min"] is not None and activity["t_max"] is not None:
        if activity["t_min"] <= c.temp <= activity["t_max"]:
            score *= 1.0
        else:
            # linear-ish penalty for being outside the band
            d = min(abs(c.temp - activity["t_min"]), abs(c.temp - activity["t_max"]))
            score *= max(0.1, 1.0 - d / 15.0)
    return score


# ---------- rules: blocked? activation factor? ----------

def active_condition_values(c: Conditions) -> set[tuple[str, str]]:
    vals: set[tuple[str, str]] = set()
    vals.add(("season", c.season))
    vals.add(("daytime", "daylight" if c.daytime else "dark"))
    if c.weather_main:
        vals.add(("weather", c.weather_main))
    if c.temp is not None:
        if c.temp < 8:
            vals.add(("temp", "cold"))
        elif c.temp > 24:
            vals.add(("temp", "hot"))
        else:
            vals.add(("temp", "mild"))
    return vals


def rule_effect(activity_id: int, rules: list[dict], c: Conditions) -> tuple[bool, float]:
    """Return (blocked, multiplicative_factor) from matching rules."""
    active = active_condition_values(c)
    blocked = False
    factor = 1.0
    for r in rules:
        if r["activity_id"] != activity_id:
            continue
        if (r["cond_type"], r["cond_value"]) not in active:
            continue
        if r["kind"] == "block":
            blocked = True
        elif r["kind"] in ("activate", "modulate"):
            factor *= r["weight"]
    return blocked, factor


# ---------- utility + routing ----------

@dataclass
class Scored:
    activity_id: int
    name: str
    cluster: str
    utility: float
    pref: float
    fit: float
    prog: float
    explore: float
    blocked: bool
    route: str
    reason: str


def utility(
    activity: dict,
    c: Conditions,
    rules: list[dict],
    n_a: int,
    n_total: int,
    goal_gap: float,
) -> Scored:
    blocked, factor = rule_effect(activity["id"], rules, c)
    pref = beta_mean(activity["alpha"], activity["beta"])
    f = fit(activity, c)
    expl = explore_ucb(n_a, n_total)
    u = (
        settings.w_pref * pref
        + settings.w_fit * f
        + settings.w_prog * goal_gap
        + settings.w_expl * expl
    ) * factor
    if blocked:
        u = 0.0

    route = _route(activity, blocked, f, pref, n_a)
    reason = _reason(activity["name"], blocked, f, pref, goal_gap, expl, factor)
    return Scored(
        activity_id=activity["id"],
        name=activity["name"],
        cluster=activity["cluster"],
        utility=round(u, 4),
        pref=round(pref, 3),
        fit=round(f, 3),
        prog=round(goal_gap, 3),
        explore=round(expl, 3),
        blocked=blocked,
        route=route,
        reason=reason,
    )


def _route(activity: dict, blocked: bool, f: float, pref: float, n_a: int) -> str:
    if not activity.get("active", 1):
        return "ARCHIVE"
    if blocked or f < 0.2:
        return "PARK"
    if n_a >= 5 and pref < 0.25:
        return "ARCHIVE"
    if f >= 0.6:
        return "NOW"
    return "NEXT"


def _reason(name, blocked, f, pref, goal_gap, expl, factor) -> str:
    if blocked:
        return f"{name}: blocked by current conditions."
    bits = []
    if factor > 1.05:
        bits.append("conditions favor it")
    if pref >= 0.6:
        bits.append("you rate it highly")
    if goal_gap > 0.3:
        bits.append("behind on goal cadence")
    if expl >= 0.8:
        bits.append("under-explored")
    if f < 0.4:
        bits.append("only a partial fit today")
    return f"{name}: " + ("; ".join(bits) if bits else "neutral fit") + "."


def select(scored: list[Scored], k: int = 1) -> list[Scored]:
    """Mostly exploit; for k>=2 enforce one pick per cluster, then Thompson swap."""
    pool = [s for s in scored if not s.blocked and s.route in ("NOW", "NEXT")]
    pool.sort(key=lambda s: s.utility, reverse=True)
    if not pool:
        return []
    if k == 1:
        picks = pool[:1]
    else:
        # One per cluster (highest-utility wins within each cluster), then fill
        picks: list[Scored] = []
        seen: set[str] = set()
        for s in pool:
            if s.cluster not in seen:
                picks.append(s)
                seen.add(s.cluster)
            if len(picks) == k:
                break
        # Fill remaining slots if fewer than k distinct clusters
        remainder = [s for s in pool if s not in picks]
        picks += remainder[:k - len(picks)]
    # Thompson exploration swap: replace last pick with most-unexplored candidate
    if random.random() < settings.explore_ratio:
        non_picks = [s for s in pool if s not in picks]
        if non_picks:
            cand = max(non_picks, key=lambda s: s.explore)
            picks[-1] = cand
    return picks
