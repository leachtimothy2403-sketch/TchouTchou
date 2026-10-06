"""
Confidence tiers for reliability numbers -- implements the 2026-09-13 alpha-readiness
recommendation (see tchoutchou_monetization_mvp.md): never show a bare per-train
percentage. Every reliability payload carries:

  - a tier, five levels by number of recorded trips (default thresholds in brackets):
        insufficient (< 5)  ->  early (5-9)  ->  emerging (10-19)  ->  solid (20-49)  ->  high (50+)
    The 95% interval on an on-time rate is roughly +/-30 points at 5-9 trips, +/-20 at
    ~20, +/-13 at ~50 and +/-9 at ~100, which is what the steps are meant to track.
  - a 95% Wilson interval on the on-time rate (honest even at small n -- a 0-for-25 train
    is already provably bad, a 15-of-20 train's exact rank is still shaky)
  - a train-type-level fallback (e.g. all TER, all TGV INOUI), which is robust today
    (tens of thousands of observations per type), plus a `headline` that says which of
    the two the UI should lead with.

The thresholds live here, server-side, so the web UI and the mobile app can never
disagree about what counts as "enough data".
"""
import math
import os
import threading
import time

TIERS = ["insufficient", "early", "emerging", "solid", "high"]
DEFAULT_THRESHOLDS = (5, 10, 20, 50)  # lower bound of early, emerging, solid, high


def thresholds():
    """Lower bounds of tiers 1..4. Override with TCHOUTCHOU_TIER_THRESHOLDS="5,10,20,50"
    (four ascending whole numbers); anything malformed falls back to the defaults."""
    raw = os.environ.get("TCHOUTCHOU_TIER_THRESHOLDS", "")
    try:
        vals = tuple(int(x) for x in raw.split(",")) if raw.strip() else DEFAULT_THRESHOLDS
    except ValueError:
        return DEFAULT_THRESHOLDS
    if len(vals) != len(TIERS) - 1 or any(b <= a for a, b in zip(vals, vals[1:])) or vals[0] < 1:
        return DEFAULT_THRESHOLDS
    return vals


TYPE_CACHE_TTL_SECONDS = 3600  # train_stats only changes once a night (aggregate.py), so
    # hourly is plenty; the GROUP BY across ~20k trains is cheap but not free per request.


def wilson_interval(successes, n, z=1.96):
    """95% Wilson score interval for a binomial proportion, as percentages. Same method
    as analyze_tchoutchou.py (not the naive +/- margin, which is wrong at small n and at
    rates near 0%/100%)."""
    if not n:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half) * 100, 1), round(min(1.0, centre + half) * 100, 1)]


def level_for(observations):
    n = observations or 0
    return sum(1 for t in thresholds() if n >= t)


def tier_for(observations):
    return TIERS[level_for(observations)]


def confidence_block(observations, on_time_count):
    return {
        "tier": tier_for(observations),
        "level": level_for(observations),   # 0..4
        "levels": len(TIERS),
        "observations": observations or 0,
        "on_time_ci95": wilson_interval(on_time_count or 0, observations or 0),
        "thresholds": dict(zip(TIERS[1:], thresholds())),
    }


# ---------------------------------------------------------------------------
# Train-type-level stats (the robust fallback layer)
# ---------------------------------------------------------------------------

_type_cache = {"at": 0.0, "data": {}}
_type_lock = threading.Lock()


def _load_type_stats(conn):
    rows = conn.execute(
        "SELECT t.train_type AS train_type, COUNT(DISTINCT s.train_number) AS n_trains, "
        "SUM(s.observations) AS obs, SUM(s.cancelled_count) AS cancelled, "
        "SUM(s.sum_final_delay) AS sum_delay, SUM(s.on_time_count) AS on_time, "
        "SUM(s.late_5_count) AS late5, SUM(s.late_15_count) AS late15, "
        "SUM(s.late_30_count) AS late30 "
        "FROM train_stats s JOIN trains t ON t.train_number = s.train_number "
        "WHERE t.train_type IS NOT NULL GROUP BY t.train_type"
    ).fetchall()
    out = {}
    for r in rows:
        obs = r["obs"] or 0
        if not obs:
            continue
        out[r["train_type"]] = {
            "train_type": r["train_type"],
            "n_trains": r["n_trains"],
            "observations": obs,
            "cancelled_count": r["cancelled"] or 0,
            "mean_delay_minutes": round((r["sum_delay"] or 0) / obs / 60, 1),
            "on_time_pct": round((r["on_time"] or 0) / obs * 100, 1),
            "late_5_pct": round((r["late5"] or 0) / obs * 100, 1),
            "late_15_pct": round((r["late15"] or 0) / obs * 100, 1),
            "late_30_pct": round((r["late30"] or 0) / obs * 100, 1),
            "on_time_ci95": wilson_interval(r["on_time"] or 0, obs),
        }
    return out


def type_stats(conn, train_type):
    if not train_type:
        return None
    now = time.time()
    with _type_lock:
        if now - _type_cache["at"] > TYPE_CACHE_TTL_SECONDS:
            _type_cache["data"] = _load_type_stats(conn)
            _type_cache["at"] = now
        return _type_cache["data"].get(train_type)


def reset_cache():
    """For tests."""
    with _type_lock:
        _type_cache["at"] = 0.0
        _type_cache["data"] = {}


def headline(train_overall, train_conf, type_fallback):
    """Which number should the UI lead with? The train's own figure once it has any
    usable history (tier above 'insufficient'); otherwise the train-type figure, clearly
    labelled as such; otherwise nothing (the UI shows 'Not enough data')."""
    if train_overall and train_conf["tier"] != "insufficient":
        return {"source": "train", "on_time_pct": train_overall["on_time_pct"],
                "tier": train_conf["tier"], "observations": train_conf["observations"]}
    if type_fallback:
        return {"source": "train_type", "on_time_pct": type_fallback["on_time_pct"],
                "train_type": type_fallback["train_type"],
                "tier": tier_for(type_fallback["observations"]),
                "observations": type_fallback["observations"]}
    return {"source": None, "on_time_pct": None, "tier": "insufficient",
            "observations": train_conf["observations"] if train_conf else 0}
