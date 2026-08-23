#!/usr/bin/env python3
"""
One-time backfill for train_station_stats / station_stats / train_stats rows that were
corrupted by the duplicate-stop-in-one-poll bug in aggregate.py's PRE-FIX "final delay
per stop" resolution (see aggregate.py's comment above final_by_stop, and
tchoutchou_r2_storage.md for the full investigation). Deploying the fix to aggregate.py
only prevents NEW corruption -- aggregation_state's idempotency gate means an
already-aggregated trip is never reprocessed, so rows that were already summed using the
old (undefined-tiebreak) resolution stay wrong until repaired here. verify_aggregate.py
confirmed this directly: 16 hard mismatches, unchanged before/after deploying the fix,
because the fix has nothing to reprocess for already-aggregated trips.

TIME-SENSITIVE. A key can only be safely repaired while EVERY trip that has ever
contributed to it still has its raw trip_updates/stop_time_updates rows present (i.e.
purge_raw.py hasn't deleted them yet). Once a contributing trip's raw data is gone,
there's no way to recover what it actually contributed -- run this BEFORE the next
purge_raw.py run, ideally right after deploying the aggregate.py fix.

Method (same overall shape as verify_aggregate.py's recompute, but using the FIXED
resolution algorithm -- the intent here is to reproduce exactly what a clean re-run of
the fixed aggregate.py would have written, not to cross-check with a deliberately
different method):

  1. Recompute every currently-checkable trip's final_by_stop using the same two-pass
     algorithm as the fixed aggregate.py (same-snapshot duplicates collapsed by larger
     delay, then per-field latest-non-null across polls), and fold the results into
     from-scratch per-key totals for train_station_stats/station_stats/train_stats.
  2. For each key, compare the recomputed "observations" count (the one column that's
     ALWAYS incremented once per contributing trip, regardless of null delays -- see
     each table's INSERT in aggregate.py) against the currently PERSISTED observations
     count on that row.
       - EQUAL: every trip that has ever contributed to this key is still visible in
         today's raw data -- nothing is missing, so the full recomputed row is provably
         correct and replaces the persisted one entirely (all sum/count columns,
         updated_at_utc bumped).
       - Recomputed LOWER than persisted: at least one contributing trip's raw data has
         already been purged, so its true contribution can't be recovered. Left
         untouched, reported as unrepairable-for-now rather than guessed at.
       - Recomputed HIGHER than persisted: not the known bug pattern -- also left
         untouched and flagged for manual review rather than blindly applied.

--dry-run (default): only prints what would change, applies nothing.
--apply: performs the UPDATEs, all inside a single transaction.

Usage:
    python repair_mismatches.py --db tchoutchou.db                # dry run, discovers everything itself
    python repair_mismatches.py --db tchoutchou.db --trains 117137,44215   # narrow scope
    python repair_mismatches.py --db tchoutchou.db --apply        # actually write the corrections
"""

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from collections import defaultdict

ON_TIME_THRESHOLD_SEC = 5 * 60
LATE_5_THRESHOLD_SEC = 5 * 60
LATE_15_THRESHOLD_SEC = 15 * 60
LATE_30_THRESHOLD_SEC = 30 * 60

_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")

# Column layouts, matching each table's real schema order (same lists as
# verify_aggregate.py). Index 0 is always "observations" -- the one column incremented
# unconditionally per contributing trip -- used as the completeness signal below.
TSS_COLUMNS = ["observations", "arrival_observations", "sum_arrival_delay", "sum_arrival_delay_sq",
               "departure_observations", "sum_departure_delay", "sum_departure_delay_sq",
               "on_time_count", "late_5_count", "late_15_count", "late_30_count"]
TSS_KEY_COLS = ["train_number", "station_uic", "direction_id", "day_type"]

STATION_COLUMNS = ["observations", "sum_arrival_delay", "sum_arrival_delay_sq",
                    "on_time_count", "late_5_count", "late_15_count", "late_30_count"]
STATION_KEY_COLS = ["station_uic", "train_type", "day_type"]

TRAIN_COLUMNS = ["observations", "sum_final_delay", "sum_final_delay_sq",
                  "on_time_count", "late_5_count", "late_15_count", "late_30_count"]
TRAIN_KEY_COLS = ["train_number", "day_type"]


def day_type_for(start_date):
    d = datetime.strptime(start_date, "%Y%m%d").date()
    wd = d.weekday()
    if wd == 5:
        return "saturday"
    if wd == 6:
        return "sunday"
    return "weekday"


def bucket_counts(delay_seconds):
    if delay_seconds is None:
        return 0, 0, 0, 0
    return (
        1 if delay_seconds < ON_TIME_THRESHOLD_SEC else 0,
        1 if delay_seconds >= LATE_5_THRESHOLD_SEC else 0,
        1 if delay_seconds >= LATE_15_THRESHOLD_SEC else 0,
        1 if delay_seconds >= LATE_30_THRESHOLD_SEC else 0,
    )


def extract_uic(stop_id):
    m = _STOP_UIC_RE.search(stop_id or "")
    return m.group(1) if m else None


def resolve_final_by_stop(conn, trip_id, start_date):
    """Exactly the fixed aggregate.py algorithm: pass 1 collapses same-snapshot
    duplicate stop_id rows keeping whichever arrival/departure pair has the larger
    non-null delay; pass 2 resolves across different polls per-field, latest-non-null
    wins (not a whole-tuple overwrite)."""
    rows = conn.execute(
        "SELECT s.id, s.fetched_at_utc, stu.stop_id, stu.arrival_delay, stu.arrival_time, "
        "stu.departure_delay, stu.departure_time "
        "FROM stop_time_updates stu "
        "JOIN trip_updates tu ON tu.id = stu.trip_update_id "
        "JOIN snapshots s ON s.id = tu.snapshot_id "
        "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc ASC, s.id ASC",
        (trip_id, start_date),
    ).fetchall()

    per_snapshot = {}
    snapshot_order = []
    for snap_id, fetched_at, stop_id, arrival_delay, arrival_time, departure_delay, departure_time in rows:
        key = (snap_id, stop_id)
        if key not in per_snapshot:
            per_snapshot[key] = [arrival_delay, arrival_time, departure_delay, departure_time]
            snapshot_order.append((fetched_at, snap_id, stop_id))
        else:
            cv = per_snapshot[key]
            if departure_delay is not None and (cv[2] is None or departure_delay > cv[2]):
                cv[2], cv[3] = departure_delay, departure_time
            if arrival_delay is not None and (cv[0] is None or arrival_delay > cv[0]):
                cv[0], cv[1] = arrival_delay, arrival_time

    final_by_stop = {}
    for fetched_at, snap_id, stop_id in snapshot_order:
        arrival_delay, arrival_time, departure_delay, departure_time = per_snapshot[(snap_id, stop_id)]
        cv = final_by_stop.get(stop_id, (None, None, None, None))
        final_by_stop[stop_id] = (
            arrival_delay if arrival_delay is not None else cv[0],
            arrival_time if arrival_time is not None else cv[1],
            departure_delay if departure_delay is not None else cv[2],
            departure_time if departure_time is not None else cv[3],
        )
    return final_by_stop


def recompute_identity(conn, trip_id, start_date):
    return conn.execute(
        "SELECT direction_id, train_type FROM trip_updates tu JOIN snapshots s ON s.id = tu.snapshot_id "
        "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc ASC, s.id ASC",
        (trip_id, start_date),
    ).fetchall()[-1]


def recompute_all(conn, checkable_trips):
    recomputed_tss = defaultdict(lambda: [0] * len(TSS_COLUMNS))
    recomputed_station = defaultdict(lambda: [0] * len(STATION_COLUMNS))
    recomputed_train = defaultdict(lambda: [0] * len(TRAIN_COLUMNS))
    checked = 0

    for trip_id, start_date, train_number in checkable_trips:
        direction_id, train_type = recompute_identity(conn, trip_id, start_date)
        direction_id_val = direction_id if direction_id is not None else -1
        train_type_bucket = train_type or "unknown"
        day_type = day_type_for(start_date)

        final_by_stop = resolve_final_by_stop(conn, trip_id, start_date)
        if not final_by_stop:
            continue
        checked += 1

        for stop_id, (arrival_delay, _at, departure_delay, _dt) in final_by_stop.items():
            station_uic = extract_uic(stop_id)
            if station_uic is None:
                continue
            ot, l5, l15, l30 = bucket_counts(arrival_delay)
            arr_present = 1 if arrival_delay is not None else 0
            dep_present = 1 if departure_delay is not None else 0
            arr_sq = arrival_delay ** 2 if arrival_delay is not None else 0
            dep_sq = departure_delay ** 2 if departure_delay is not None else 0

            e = recomputed_tss[(train_number, station_uic, direction_id_val, day_type)]
            e[0] += 1
            e[1] += arr_present
            e[2] += arrival_delay or 0
            e[3] += arr_sq
            e[4] += dep_present
            e[5] += departure_delay or 0
            e[6] += dep_sq
            e[7] += ot
            e[8] += l5
            e[9] += l15
            e[10] += l30

            se = recomputed_station[(station_uic, train_type_bucket, day_type)]
            se[0] += arr_present
            se[1] += arrival_delay or 0
            se[2] += arr_sq
            se[3] += ot
            se[4] += l5
            se[5] += l15
            se[6] += l30

        def _order_time(v):
            _ad, at, _dd, dt = v
            t = at if at is not None else dt
            return t if t is not None else -1

        _last_stop, (fad, _fat, fdd, _fdt) = max(final_by_stop.items(), key=lambda kv: _order_time(kv[1]))
        trip_final_delay = fad if fad is not None else fdd
        ot, l5, l15, l30 = bucket_counts(trip_final_delay)
        te = recomputed_train[(train_number, day_type)]
        te[0] += 1
        te[1] += trip_final_delay or 0
        te[2] += (trip_final_delay ** 2 if trip_final_delay is not None else 0)
        te[3] += ot
        te[4] += l5
        te[5] += l15
        te[6] += l30

    print(f"Recomputed from {checked} currently-checkable trip(s).")
    return recomputed_tss, recomputed_station, recomputed_train


def repair_table(conn, table, key_cols, columns, recomputed_dict, apply, max_report):
    complete = []
    incomplete = []
    unchanged = 0
    anomalies = []

    for key, recomputed in recomputed_dict.items():
        where = " AND ".join(f"{c}=?" for c in key_cols)
        actual = conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", key).fetchone()
        if actual is None:
            anomalies.append((key, "row missing entirely -- not auto-repaired, needs manual review"))
            continue

        persisted_obs = actual[0]
        recomputed_obs = recomputed[0]
        if recomputed_obs < persisted_obs:
            incomplete.append((key, recomputed_obs, persisted_obs))
            continue
        if recomputed_obs > persisted_obs:
            anomalies.append((key, f"recomputed observations={recomputed_obs} > persisted={persisted_obs} "
                                    f"-- not the known bug pattern, needs manual review"))
            continue

        if list(actual) == list(recomputed):
            unchanged += 1
            continue
        complete.append((key, list(actual), recomputed))

    print(f"\n{table}: {len(recomputed_dict)} key(s) recomputed.")
    print(f"  {unchanged} already correct.")
    print(f"  {len(complete)} key(s) fully repairable now (all contributing trips still have raw data):")
    for key, before, after in complete[:max_report]:
        print(f"    key={dict(zip(key_cols, key))}")
        diffs = {columns[i]: (before[i], after[i]) for i in range(len(columns)) if before[i] != after[i]}
        print(f"      changed columns (before -> after): {diffs}")
    if len(complete) > max_report:
        print(f"    ... and {len(complete) - max_report} more (raise --max-report to see them)")

    print(f"  {len(incomplete)} key(s) NOT repairable yet -- some contributing trip's raw data is "
          f"already purged:")
    for key, recomputed_obs, persisted_obs in incomplete[:max_report]:
        print(f"    key={dict(zip(key_cols, key))}: recomputed observations={recomputed_obs} < "
              f"persisted={persisted_obs} (missing {persisted_obs - recomputed_obs} historical "
              f"observation(s) we can no longer see)")
    if len(incomplete) > max_report:
        print(f"    ... and {len(incomplete) - max_report} more")

    if anomalies:
        print(f"  {len(anomalies)} anomaly/anomalies (not auto-repaired):")
        for key, msg in anomalies[:max_report]:
            print(f"    key={dict(zip(key_cols, key))}: {msg}")

    if apply and complete:
        now = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{c}=?" for c in columns) + ", updated_at_utc=?"
        where = " AND ".join(f"{c}=?" for c in key_cols)
        for key, _before, after in complete:
            conn.execute(f"UPDATE {table} SET {set_clause} WHERE {where}", (*after, now, *key))
        print(f"  Applied {len(complete)} correction(s) to {table}.")

    return len(complete), len(incomplete), len(anomalies)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--apply", action="store_true", help="Actually write corrections (default: dry run)")
    ap.add_argument("--max-report", type=int, default=25)
    ap.add_argument("--trains", default=None, help="Comma-separated train_numbers to limit the run to")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)

    where_train = ""
    params = ()
    if args.trains:
        wanted = [t.strip() for t in args.trains.split(",") if t.strip()]
        placeholders = ",".join("?" for _ in wanted)
        where_train = f"AND ags.train_number IN ({placeholders})"
        params = tuple(wanted)

    checkable_trips = conn.execute(
        "SELECT ags.trip_id, ags.start_date, ags.train_number "
        "FROM aggregation_state ags "
        "WHERE ags.cancelled = 0 AND ags.train_number IS NOT NULL "
        f"{where_train} "
        "AND EXISTS (SELECT 1 FROM trip_updates tu WHERE tu.trip_id=ags.trip_id AND tu.start_date=ags.start_date)",
        params,
    ).fetchall()

    print(f"=== repair_mismatches.py against {args.db} ({'APPLY' if args.apply else 'DRY RUN'}) ===")
    print(f"{len(checkable_trips)} aggregated trip(s) currently have raw data available to recompute from.")
    if not checkable_trips:
        print("Nothing to do -- no checkable trips right now.")
        conn.close()
        return

    recomputed_tss, recomputed_station, recomputed_train = recompute_all(conn, checkable_trips)

    total_complete = 0
    total_incomplete = 0
    total_anomalies = 0
    for table, key_cols, columns, recomputed in (
        ("train_station_stats", TSS_KEY_COLS, TSS_COLUMNS, recomputed_tss),
        ("station_stats", STATION_KEY_COLS, STATION_COLUMNS, recomputed_station),
        ("train_stats", TRAIN_KEY_COLS, TRAIN_COLUMNS, recomputed_train),
    ):
        c, i, a = repair_table(conn, table, key_cols, columns, recomputed, args.apply, args.max_report)
        total_complete += c
        total_incomplete += i
        total_anomalies += a

    if args.apply:
        conn.commit()
        print(f"\nCommitted. {total_complete} key(s) repaired.")
    else:
        print(f"\nDry run only -- nothing written. {total_complete} key(s) would be repaired, "
              f"{total_incomplete} not repairable yet (raw already purged), "
              f"{total_anomalies} anomaly/anomalies needing manual review.")
        print("Re-run with --apply to write the corrections.")

    conn.close()


if __name__ == "__main__":
    main()
