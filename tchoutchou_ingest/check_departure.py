#!/usr/bin/env python3
"""
Follow-up to check_write_timing.py -- that script only pulled arrival_delay and only
printed a subset of the train_station_stats row's columns, missing the departure fields
that were actually flagged. This gets everything in one pass: per matching trip, both
arrival_delay AND departure_delay under aggregate.py's real resolution method, plus the
row's full departure columns.

Usage:
    python check_departure.py --db tchoutchou.db --train 117137 --station 87113001
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
        "WHERE train_number=? AND cancelled=0 ORDER BY aggregated_at_utc",
        (args.train,),
    ).fetchall()

    for trip_id, start_date, aggregated_at in trips:
        rows = conn.execute(
            "SELECT stu.stop_id, stu.arrival_delay, stu.departure_delay, s.fetched_at_utc, s.id "
            "FROM stop_time_updates stu "
            "JOIN trip_updates tu ON tu.id = stu.trip_update_id "
            "JOIN snapshots s ON s.id = tu.snapshot_id "
            "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc ASC, s.id ASC",
            (trip_id, start_date),
        ).fetchall()
        matching = [r for r in rows if extract_uic(r[0]) == args.station]
        if not matching:
            print(f"trip start_date={start_date} aggregated_at_utc={aggregated_at}: no stop at this station")
            continue
        print(f"\ntrip start_date={start_date} aggregated_at_utc={aggregated_at}: "
              f"{len(matching)} raw poll(s) for this stop")
        print(f"  {'fetched_at_utc':<30} {'snap_id':>8} {'arrival_delay':>14} {'departure_delay':>16}")
        for stop_id, arr, dep, fetched, snap_id in matching:
            print(f"  {fetched:<30} {snap_id:>8} {str(arr):>14} {str(dep):>16}")
        # aggregate.py's real resolution: ascending order, whole-tuple overwrite, last wins
        final_arr, final_dep = matching[-1][1], matching[-1][2]
        print(f"  => final (last poll wins): arrival_delay={final_arr}, departure_delay={final_dep}")

    print(f"\n--- persisted train_station_stats row(s) for train={args.train}, station={args.station} ---")
    direction_ids = conn.execute(
        "SELECT DISTINCT direction_id FROM train_station_stats WHERE train_number=? AND station_uic=?",
        (args.train, args.station),
    ).fetchall()
    for (direction_id,) in direction_ids:
        rows = conn.execute(
            "SELECT day_type, observations, arrival_observations, sum_arrival_delay, sum_arrival_delay_sq, "
            "departure_observations, sum_departure_delay, sum_departure_delay_sq, "
            "on_time_count, late_5_count, late_15_count, late_30_count, updated_at_utc "
            "FROM train_station_stats WHERE train_number=? AND station_uic=? AND direction_id=?",
            (args.train, args.station, direction_id),
        ).fetchall()
        for r in rows:
            (day_type, obs, arr_obs, sum_arr, sum_arr_sq, dep_obs, sum_dep, sum_dep_sq,
             ot, l5, l15, l30, updated_at) = r
            print(f"direction_id={direction_id} day_type={day_type}")
            print(f"  observations={obs} arrival_observations={arr_obs} sum_arrival_delay={sum_arr} "
                  f"sum_arrival_delay_sq={sum_arr_sq}")
            print(f"  departure_observations={dep_obs} sum_departure_delay={sum_dep} "
                  f"sum_departure_delay_sq={sum_dep_sq}")
            print(f"  on_time={ot} late5={l5} late15={l15} late30={l30} updated_at_utc={updated_at}")

    conn.close()


if __name__ == "__main__":
    main()
