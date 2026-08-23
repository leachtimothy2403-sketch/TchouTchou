#!/usr/bin/env python3
"""
Drills into ONE flagged (train_number[, station_uic]) mismatch from verify_aggregate.py
and shows the full raw poll history behind it, so the actual root cause is visible
instead of just "actual != recomputed".

Working hypothesis this is built to test (from reading parse.py): a single GTFS-RT poll
often reports arrival OR departure delay for a stop, not always both -- whichever field
the feed omits comes back as NULL for that poll. aggregate.py's process_trip() picks the
final observation per stop with:

    for stop_id, ... in cur.fetchall():   # ascending fetched_at_utc
        final_by_stop[stop_id] = (arrival_delay, arrival_time, departure_delay, departure_time)

That overwrites the WHOLE 4-tuple every time a newer poll still reports the stop --
including replacing a real, earlier-captured departure_delay with None, if the newer
poll happens to omit departure for that stop while still including it for arrival (e.g.
departure already happened and dropped out of the feed's "predicted" fields, while
arrival to the NEXT stop hasn't -- or any number of feed-specific reasons). The result:
a real departure delay gets silently discarded in favor of a later poll's NULL, purely
because "later" was compared at the whole-row level instead of per-field.

This script computes THREE things per stop, per trip, so the difference (if any) is
directly visible:
  1. "prod" -- literal port of aggregate.py's actual method (whole-row overwrite,
     ascending fetched_at_utc).
  2. "latest_nonnull" -- the hypothesis fix: independently, per field, take the value
     from the latest poll where THAT field specifically is non-NULL.
  3. The full raw poll-by-poll table for that stop, so you can see it with your own eyes
     rather than trust either computed answer.

Usage:
    python inspect_mismatch.py --db tchoutchou.db --train 117137 --station 87113001
    python inspect_mismatch.py --db tchoutchou.db --train 44215 --station 87640912
    python inspect_mismatch.py --db tchoutchou.db --train 117137   # all stations for this train
"""

import argparse
import re
import sqlite3

_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")


def extract_uic(stop_id):
    m = _STOP_UIC_RE.search(stop_id or "")
    return m.group(1) if m else None


def prod_final_by_stop(rows):
    """Literal port of aggregate.py's process_trip(): ascending fetched_at_utc,
    whole-tuple dict overwrite. `rows` must already be sorted ascending by
    (fetched_at_utc, snapshot id) -- same as aggregate.py's own query ORDER BY."""
    final = {}
    for stop_id, arrival_delay, arrival_time, departure_delay, departure_time, _fetched, _snap_id in rows:
        final[stop_id] = (arrival_delay, arrival_time, departure_delay, departure_time)
    return final


def latest_nonnull_final_by_stop(rows):
    """Hypothesis fix: same ascending order, but each of the 4 fields is only
    overwritten when the NEW value for THAT field is non-NULL -- a later poll that
    omits departure_delay can't erase an earlier poll's real departure_delay anymore."""
    final = {}
    for stop_id, arrival_delay, arrival_time, departure_delay, departure_time, _fetched, _snap_id in rows:
        cur = final.get(stop_id, (None, None, None, None))
        final[stop_id] = (
            arrival_delay if arrival_delay is not None else cur[0],
            arrival_time if arrival_time is not None else cur[1],
            departure_delay if departure_delay is not None else cur[2],
            departure_time if departure_time is not None else cur[3],
        )
    return final


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--train", required=True, help="commercial_train_number, e.g. 117137")
    ap.add_argument("--station", default=None, help="station UIC to focus on, e.g. 87113001 (omit for all)")
    ap.add_argument("--max-trips", type=int, default=5, help="Max differing trips to show full detail for")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    trips = conn.execute(
        "SELECT trip_id, start_date FROM aggregation_state "
        "WHERE train_number=? AND cancelled=0 "
        "AND EXISTS (SELECT 1 FROM trip_updates tu WHERE tu.trip_id=aggregation_state.trip_id "
        "AND tu.start_date=aggregation_state.start_date)",
        (args.train,),
    ).fetchall()
    print(f"Train {args.train}: {len(trips)} aggregated trip(s) still have raw data.\n")

    shown = 0
    for trip_id, start_date in trips:
        rows = conn.execute(
            "SELECT stu.stop_id, stu.arrival_delay, stu.arrival_time, stu.departure_delay, "
            "stu.departure_time, s.fetched_at_utc, s.id "
            "FROM stop_time_updates stu "
            "JOIN trip_updates tu ON tu.id = stu.trip_update_id "
            "JOIN snapshots s ON s.id = tu.snapshot_id "
            "WHERE tu.trip_id=? AND tu.start_date=? "
            "ORDER BY s.fetched_at_utc ASC, s.id ASC",
            (trip_id, start_date),
        ).fetchall()
        if not rows:
            continue

        by_stop = {}
        for r in rows:
            by_stop.setdefault(r[0], []).append(r)

        prod = prod_final_by_stop(rows)
        alt = latest_nonnull_final_by_stop(rows)

        diffs = []
        for stop_id in by_stop:
            station_uic = extract_uic(stop_id)
            if args.station and station_uic != args.station:
                continue
            if prod.get(stop_id) != alt.get(stop_id):
                diffs.append(stop_id)

        if not diffs:
            continue
        shown += 1
        if shown > args.max_trips:
            print(f"... more differing trips exist, raise --max-trips to see them")
            break

        print(f"=== trip_id={trip_id} start_date={start_date} -- {len(diffs)} stop(s) differ ===")
        for stop_id in diffs:
            station_uic = extract_uic(stop_id)
            print(f"\n  stop_id={stop_id} (station_uic={station_uic})")
            print(f"    prod (aggregate.py's actual method) final:  arrival_delay={prod[stop_id][0]}, "
                  f"departure_delay={prod[stop_id][2]}")
            print(f"    latest_nonnull (per-field) final:           arrival_delay={alt[stop_id][0]}, "
                  f"departure_delay={alt[stop_id][2]}")
            print(f"    raw poll history for this stop, ascending:")
            print(f"      {'fetched_at_utc':<28} {'snap_id':>8} {'arr_delay':>10} {'dep_delay':>10}")
            for r in by_stop[stop_id]:
                _sid, arr_d, _at, dep_d, _dt, fetched, snap_id = r
                print(f"      {fetched:<28} {snap_id:>8} {str(arr_d):>10} {str(dep_d):>10}")
        print()

    if shown == 0:
        print("No stop found where the two methods disagree for this train"
              + (f"/station {args.station}" if args.station else "")
              + " -- the hypothesis in this script's docstring doesn't explain this "
                "particular mismatch, needs a different theory.")

    conn.close()


if __name__ == "__main__":
    main()
