#!/usr/bin/env python3
"""
Dumps the COMPLETE stop_time_update list for one specific trip_update_id, in insertion
order (stop_time_updates.id, a reasonable proxy for original protobuf list order since
parse.py inserts them in one straight loop per poll -- see trip_update_row() in
parse.py). Answers: does the flagged station appear at two genuinely different POSITIONS
in the route (consistent with a real there-and-back/technical-reversal visit), or right
next to itself (consistent with a plain list-duplication glitch)?

Usage:
    python check_full_itinerary.py --db tchoutchou.db --trip-update-id 1244218
"""
import argparse
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--trip-update-id", type=int, required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    tu = conn.execute(
        "SELECT tu.trip_id, tu.start_date, tu.entity_id, tu.stop_time_update_count, s.fetched_at_utc "
        "FROM trip_updates tu JOIN snapshots s ON s.id = tu.snapshot_id WHERE tu.id=?",
        (args.trip_update_id,),
    ).fetchone()
    if tu is None:
        print("trip_update_id not found")
        return
    trip_id, start_date, entity_id, stu_count, fetched_at = tu
    print(f"trip_update_id={args.trip_update_id} trip_id={trip_id} start_date={start_date} "
          f"entity_id={entity_id} fetched_at_utc={fetched_at}")
    print(f"stop_time_update_count recorded on trip_updates row: {stu_count}\n")

    rows = conn.execute(
        "SELECT id, stop_id, arrival_delay, arrival_time, departure_delay, departure_time "
        "FROM stop_time_updates WHERE trip_update_id=? ORDER BY id ASC",
        (args.trip_update_id,),
    ).fetchall()
    print(f"{len(rows)} stop_time_update row(s), in insertion order:")
    print(f"  {'#':>4} {'stu_id':>8} {'stop_id':<32} {'arr_delay':>10} {'arr_time':>12} {'dep_delay':>10} {'dep_time':>12}")
    for i, (stu_id, stop_id, arr_d, arr_t, dep_d, dep_t) in enumerate(rows):
        print(f"  {i:>4} {stu_id:>8} {stop_id:<32} {str(arr_d):>10} {str(arr_t):>12} {str(dep_d):>10} {str(dep_t):>12}")

    conn.close()


if __name__ == "__main__":
    main()
