#!/usr/bin/env python3
"""
Follow-up to inspect_mismatch.py, for the case where the two per-stop resolution
methods agree with each other but still don't match the persisted stats (ruling out
"which observation is final" as the cause). This checks a different theory: was the
matching trip's contribution actually written at all?

For a given (train, station), for each currently-checkable trip that has a stop at that
station: prints the trip's own aggregation_state.aggregated_at_utc, and what it would
contribute (arrival_delay, on_time classification). Then prints the flagged
train_station_stats row's updated_at_utc. If a trip's aggregated_at_utc is AFTER the
row's updated_at_utc, that trip's contribution cannot have been applied to that row --
direct evidence of a write that didn't happen, despite aggregation_state saying it did.

Usage:
    python check_write_timing.py --db tchoutchou.db --train 117137 --station 87113001
"""
import argparse
import re
import sqlite3

_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")


def extract_uic(stop_id):
    m = _STOP_UIC_RE.search(stop_id or "")
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--train", required=True)
    ap.add_argument("--station", required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    trips = conn.execute(
        "SELECT trip_id, start_date, aggregated_at_utc FROM aggregation_state "
        "WHERE train_number=? AND cancelled=0",
        (args.train,),
    ).fetchall()
    print(f"aggregation_state has {len(trips)} non-cancelled trip(s) for train {args.train} "
          f"(includes ones whose raw data may already be purged).\n")

    for trip_id, start_date, aggregated_at in trips:
        rows = conn.execute(
            "SELECT stu.stop_id, stu.arrival_delay, s.fetched_at_utc, s.id "
            "FROM stop_time_updates stu "
            "JOIN trip_updates tu ON tu.id = stu.trip_update_id "
            "JOIN snapshots s ON s.id = tu.snapshot_id "
            "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc ASC, s.id ASC",
            (trip_id, start_date),
        ).fetchall()
        has_raw = len(rows) > 0
        final_delay = None
        for stop_id, arrival_delay, _fetched, _snap in rows:
            if extract_uic(stop_id) == args.station:
                final_delay = arrival_delay  # last one wins, ascending order
        matched = final_delay is not None or any(extract_uic(r[0]) == args.station for r in rows)
        print(f"  trip_id={trip_id} start_date={start_date}  aggregated_at_utc={aggregated_at}  "
              f"raw_present={has_raw}  matches_station={matched}"
              + (f"  final_arrival_delay={final_delay}" if matched else ""))

    direction_ids = conn.execute(
        "SELECT DISTINCT direction_id FROM train_station_stats WHERE train_number=? AND station_uic=?",
        (args.train, args.station),
    ).fetchall()
    print(f"\ntrain_station_stats row(s) for train={args.train}, station={args.station}:")
    for (direction_id,) in direction_ids:
        row = conn.execute(
            "SELECT day_type, observations, on_time_count, late_5_count, late_15_count, "
            "late_30_count, updated_at_utc FROM train_station_stats "
            "WHERE train_number=? AND station_uic=? AND direction_id=?",
            (args.train, args.station, direction_id),
        ).fetchall()
        for day_type, obs, ot, l5, l15, l30, updated_at in row:
            print(f"  direction_id={direction_id} day_type={day_type} observations={obs} "
                  f"on_time={ot} late5={l5} late15={l15} late30={l30} updated_at_utc={updated_at}")

    conn.close()


if __name__ == "__main__":
    main()
