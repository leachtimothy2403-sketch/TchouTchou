#!/usr/bin/env python3
"""
Validates the 2026-08-18 SIRI upsert redesign against real traffic, without needing the
raw_gzip blob. Two checks:

1. REGRESSION CHECK (automatic): compares platform_calls between two monitor_snapshot.py
   exports taken a few hours apart. A confirmed platform should never legitimately go
   back to blank, and a call should never legitimately flip from 'recorded' back to
   'estimated' for the same stop -- SNCF reassigns platforms, it doesn't un-announce
   them. ingest.py's ON CONFLICT clause overwrites arrival_platform_name/
   departure_platform_name/call_type unconditionally with each poll's latest value (see
   poll_siri_feed()) rather than COALESCE-guarding them like it does for
   stop_point_name/aimed_*_time -- so a transient feed glitch (a poll that reports a
   stop without a platform right after a poll that had one) would silently erase a
   confirmed platform with no error and no trace, unless caught here.

2. SPOT-CHECK LIST (manual): prints confirmed platforms for calls aimed in the near
   future, so a human can check them against SNCF Connect / a real departure board and
   confirm they're actually right, not just internally consistent.

Usage:
    python compare_platform_snapshots.py --before monitor_t0.db --after monitor_t1.db
    python compare_platform_snapshots.py --after monitor_t1.db --spot-check-only
"""

import argparse
import sqlite3
from datetime import datetime, timezone


def load_calls(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT train_number, calendar_date, stop_point_ref, call_type, "
        "aimed_arrival_time, arrival_platform_name, "
        "aimed_departure_time, departure_platform_name, last_updated_at_utc "
        "FROM platform_calls"
    ).fetchall()
    conn.close()
    by_key = {}
    for (tn, cd, ref, call_type, aimed_arr, arr_plat, aimed_dep, dep_plat, updated) in rows:
        by_key[(tn, cd, ref)] = {
            "call_type": call_type,
            "aimed_arrival_time": aimed_arr, "arrival_platform_name": arr_plat,
            "aimed_departure_time": aimed_dep, "departure_platform_name": dep_plat,
            "last_updated_at_utc": updated,
        }
    return by_key


def regression_check(before_path, after_path):
    before = load_calls(before_path)
    after = load_calls(after_path)
    regressions = []

    for key, b in before.items():
        a = after.get(key)
        if a is None:
            continue  # stop aged out / not in the after export -- not a regression signal
        for field, platform_field in (("arrival", "arrival_platform_name"), ("departure", "departure_platform_name")):
            b_plat, a_plat = b[platform_field], a[platform_field]
            if b_plat and not a_plat:
                regressions.append((key, field, f"platform '{b_plat}' -> blank"))
        if b["call_type"] == "recorded" and a["call_type"] == "estimated":
            regressions.append((key, "call_type", "recorded -> estimated"))

    print(f"Compared {len(before)} stops (before) against {len(after)} stops (after).")
    if not regressions:
        print("No regressions found -- no confirmed platform silently went blank, "
              "no call reverted from recorded to estimated. Good sign for the upsert logic.")
    else:
        print(f"\n{len(regressions)} POTENTIAL REGRESSION(S) -- worth checking against reality:")
        for (tn, cd, ref), field, desc in regressions:
            print(f"  train {tn} ({cd}) stop {ref}: {field} {desc}")
    return regressions


def _parse_aimed(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def spot_check_list(after_path, limit=10):
    """
    Prints confirmed-platform calls near the current time, for manual verification
    against SNCF Connect / a real departure board.

    NOTE (found 2026-08-19): this used to ORDER BY aimed time DESC with no time filter --
    that doesn't mean "most recently confirmed" or "happening soon", it means "the single
    latest-scheduled timestamp anywhere in platform_calls". Since platform_calls holds
    the full current+upcoming service day (upserted to final state, not append-only), the
    max timestamp is always going to be that day's last services -- which, per
    CROSS_VALIDATION_STUCK_SUMMARY.md's feed-scope finding, are disproportionately
    Transilien/RER suburban trains running near/after midnight. That's why the list was
    all late-night alphanumeric mission-code trains (NEMO50, KJZZ62, PERO56...) instead of
    anything happening around the actual current time. Fixed to rank by closeness to now
    instead: upcoming calls first (soonest first), falling back to the most recently
    departed ones if nothing is upcoming.
    """
    conn = sqlite3.connect(f"file:{after_path}?mode=ro", uri=True)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT pj.train_number, pj.origin_name, pj.destination_name, pc.stop_point_name, "
        "pc.aimed_arrival_time, pc.arrival_platform_name, "
        "pc.aimed_departure_time, pc.departure_platform_name "
        "FROM platform_calls pc JOIN platform_journeys pj "
        "  ON pj.train_number = pc.train_number AND pj.calendar_date = pc.calendar_date "
        "WHERE pc.arrival_platform_name IS NOT NULL OR pc.departure_platform_name IS NOT NULL"
    ).fetchall()
    conn.close()

    now = datetime.now().astimezone()  # local (server) time, to compare against aimed times that carry a UTC offset

    scored = []
    for row in rows:
        tn, origin, dest, stop, aimed_arr, arr_plat, aimed_dep, dep_plat = row
        aimed = _parse_aimed(aimed_arr) or _parse_aimed(aimed_dep)
        if aimed is None:
            continue
        is_past = aimed < now
        scored.append(((is_past, abs((aimed - now).total_seconds())), row))

    scored.sort(key=lambda x: x[0])
    rows = [row for _, row in scored[:limit]]

    print(f"\n{len(rows)} confirmed-platform calls near {now.isoformat(timespec='minutes')} "
          f"to spot-check against SNCF Connect / a real departure board:")
    for (tn, origin, dest, stop, aimed_arr, arr_plat, aimed_dep, dep_plat) in rows:
        route = f"{origin or '?'} -> {dest or '?'}"
        note = "  [mission code -- won't be searchable on SNCF Connect, check a Transilien/RATP live board instead]" \
            if tn and not tn.isdigit() and "-" not in tn else ""
        if arr_plat:
            print(f"  Train {tn} ({route}): arrives {stop} at {aimed_arr} -- TchouTchou says platform {arr_plat}{note}")
        if dep_plat:
            print(f"  Train {tn} ({route}): departs {stop} at {aimed_dep} -- TchouTchou says platform {dep_plat}{note}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--before", help="Earlier monitor_snapshot.py export")
    ap.add_argument("--after", required=True, help="Later monitor_snapshot.py export")
    ap.add_argument("--spot-check-only", action="store_true", help="Skip the before/after diff, just print the spot-check list")
    ap.add_argument("--limit", type=int, default=10, help="How many calls to print for spot-checking (default 10)")
    args = ap.parse_args()

    if not args.spot_check_only:
        if not args.before:
            ap.error("--before is required unless --spot-check-only is set")
        regression_check(args.before, args.after)

    spot_check_list(args.after, args.limit)


if __name__ == "__main__":
    main()
