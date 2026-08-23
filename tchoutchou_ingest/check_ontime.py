#!/usr/bin/env python3
"""
Diagnostic for pattern (b): on_time_count undercounted at station 87640912 across
trains 44215-44253 ("Car TER" replacement bus). repair_mismatches.py already ruled out
the departure-duplicate bug as the cause here -- its recompute (using the fixed
aggregate.py's duplicate-collapse-prefer-larger-delay algorithm) reproduces the SAME
on_time_count as what's currently persisted, so whatever's wrong survives the fix.

This dumps, per trip, the raw arrival poll history at the target station under THREE
lenses side by side:
  - "fixed_algo": exactly what the deployed aggregate.py/repair_mismatches.py computes
    (same-snapshot duplicates collapsed by larger non-null delay, then per-field
    latest-non-null across different polls).
  - "true_latest": the single most-recent poll's row for this stop, full stop -- no
    duplicate collapsing at all, just "whatever the last poll said" (closest to what
    verify_aggregate.py's window function computes, modulo its own same-snapshot
    tiebreak ambiguity).
  - raw poll list itself, so any pattern (multiple distinct snapshots disagreeing,
    genuine same-snapshot duplicates, an unexpected station/stop_id variant) is visible
    directly.

If fixed_algo and true_latest agree with each other but still differ from what's
persisted, the discrepancy predates both current hypotheses and points at something in
aggregation_state/write-timing instead (same shape as the check_write_timing.py /
check_departure.py investigation for pattern (a)) -- this script prints that comparison
explicitly so it's obvious which case we're in.

Usage:
    python check_ontime.py --db tchoutchou.db --train 44215 --station 87640912
"""
import argparse
import re
import sqlite3

ON_TIME_THRESHOLD_SEC = 5 * 60
_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")


def extract_uic(stop_id):
    m = _STOP_UIC_RE.search(stop_id or "")
    return m.group(1) if m else None


def fixed_algo_final(rows):
    """rows: list of (snap_id, fetched_at, stop_id, arrival_delay, arrival_time,
    departure_delay, departure_time), in ascending (fetched_at, snap_id) order."""
    per_snapshot = {}
    snapshot_order = []
    for snap_id, fetched_at, stop_id, ad, at, dd, dt in rows:
        key = (snap_id, stop_id)
        if key not in per_snapshot:
            per_snapshot[key] = [ad, at, dd, dt]
            snapshot_order.append((fetched_at, snap_id, stop_id))
        else:
            cv = per_snapshot[key]
            if dd is not None and (cv[2] is None or dd > cv[2]):
                cv[2], cv[3] = dd, dt
            if ad is not None and (cv[0] is None or ad > cv[0]):
                cv[0], cv[1] = ad, at

    final_by_stop = {}
    for fetched_at, snap_id, stop_id in snapshot_order:
        ad, at, dd, dt = per_snapshot[(snap_id, stop_id)]
        cv = final_by_stop.get(stop_id, (None, None, None, None))
        final_by_stop[stop_id] = (
            ad if ad is not None else cv[0],
            at if at is not None else cv[1],
            dd if dd is not None else cv[2],
            dt if dt is not None else cv[3],
        )
    return final_by_stop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--train", required=True)
    ap.add_argument("--station", required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    trips = conn.execute(
        "SELECT trip_id, start_date, aggregated_at_utc FROM aggregation_state "
        "WHERE train_number=? AND cancelled=0 ORDER BY start_date",
        (args.train,),
    ).fetchall()

    for trip_id, start_date, aggregated_at in trips:
        rows = conn.execute(
            "SELECT s.id, s.fetched_at_utc, stu.stop_id, stu.arrival_delay, stu.arrival_time, "
            "stu.departure_delay, stu.departure_time "
            "FROM stop_time_updates stu "
            "JOIN trip_updates tu ON tu.id = stu.trip_update_id "
            "JOIN snapshots s ON s.id = tu.snapshot_id "
            "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc ASC, s.id ASC",
            (trip_id, start_date),
        ).fetchall()
        matching = [r for r in rows if extract_uic(r[2]) == args.station]
        if not matching:
            print(f"trip start_date={start_date} aggregated_at_utc={aggregated_at}: no stop at this station")
            continue

        print(f"\ntrip start_date={start_date} aggregated_at_utc={aggregated_at}: "
              f"{len(matching)} raw poll(s) for this stop")
        print(f"  {'snap_id':>8} {'fetched_at_utc':<30} {'stop_id':<28} {'arrival_delay':>14} {'arrival_time':>14}")
        for snap_id, fetched, stop_id, ad, at, dd, dt in matching:
            print(f"  {snap_id:>8} {fetched:<30} {stop_id:<28} {str(ad):>14} {str(at):>14}")

        fixed = fixed_algo_final(matching)
        # true_latest: single most-recent poll's row for this stop_id, no collapsing
        by_stop_latest = {}
        for snap_id, fetched, stop_id, ad, at, dd, dt in matching:
            by_stop_latest[stop_id] = (ad, at, dd, dt)  # ascending order -> last write wins -> latest poll

        for stop_id in fixed:
            f_ad = fixed[stop_id][0]
            t_ad = by_stop_latest[stop_id][0]
            f_ot = "on_time" if f_ad is not None and f_ad < ON_TIME_THRESHOLD_SEC else "late/none"
            t_ot = "on_time" if t_ad is not None and t_ad < ON_TIME_THRESHOLD_SEC else "late/none"
            agree = "AGREE" if f_ad == t_ad else "DISAGREE"
            print(f"  stop_id={stop_id}: fixed_algo arrival_delay={f_ad} ({f_ot})  "
                  f"true_latest arrival_delay={t_ad} ({t_ot})  [{agree}]")

    print(f"\n--- persisted train_station_stats row(s) for train={args.train}, station={args.station} ---")
    direction_ids = conn.execute(
        "SELECT DISTINCT direction_id FROM train_station_stats WHERE train_number=? AND station_uic=?",
        (args.train, args.station),
    ).fetchall()
    for (direction_id,) in direction_ids:
        rows = conn.execute(
            "SELECT day_type, observations, arrival_observations, sum_arrival_delay, on_time_count, "
            "late_5_count, late_15_count, late_30_count, updated_at_utc "
            "FROM train_station_stats WHERE train_number=? AND station_uic=? AND direction_id=?",
            (args.train, args.station, direction_id),
        ).fetchall()
        for day_type, obs, arr_obs, sum_arr, ot, l5, l15, l30, updated_at in rows:
            print(f"direction_id={direction_id} day_type={day_type} observations={obs} "
                  f"arrival_observations={arr_obs} sum_arrival_delay={sum_arr} "
                  f"on_time={ot} late5={l5} late15={l15} late30={l30} updated_at_utc={updated_at}")

    conn.close()


if __name__ == "__main__":
    main()
