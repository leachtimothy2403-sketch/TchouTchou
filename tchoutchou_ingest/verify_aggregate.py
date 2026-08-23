#!/usr/bin/env python3
"""
Independently recomputes what aggregate.py's permanent-layer stats SHOULD be, straight
from the raw layer, and diffs the result against what's actually stored in
train_station_stats / station_stats / train_stats / aggregation_state.

Deliberately NOT a re-run of aggregate.py's own code. If aggregate.py's "which
observation is final" resolution has a bug, calling it again would just reproduce the
same wrong answer. aggregate.py picks the final observation per stop with a Python loop
that overwrites a dict in ascending-fetched_at_utc order; this script instead uses a SQL
window function with an explicit tiebreak (fetched_at_utc DESC, snapshot id DESC) --
different mechanism, same intent -- so a real bug is likely to surface as a genuine
mismatch instead of being silently agreed with. day_type/bucket-threshold logic is also
reimplemented from scratch here rather than imported, for the same reason.

Scope, and why it shrinks over time: a trip can only be checked here if it's BOTH
already aggregated (present in aggregation_state) AND still has raw rows in
trip_updates/stop_time_updates. Once purge_raw.py deletes a trip's raw data, there's
nothing left to compare against -- this can only re-verify history still inside the
current retention window. That's why it's meant to run DAILY, right after aggregate.py
and before purge_raw.py -- every newly-aggregated trip gets checked once while its raw
data still exists, not as a one-off audit.

One check (trip-count coverage, see check_trip_counts()) doesn't depend on raw data at
all and always runs in full, regardless of retention window.

On the stats comparison specifically: "expected" here is recomputed from only the
subset of trips still inside the retention window, but "actual" reflects ALL history
ever folded in, including older trips whose raw data is already gone. So this can only
assert actual >= expected, never equality -- and only for columns that are genuinely
monotonic (observation counts, squared-delay sums, bucket counts: every trip's
contribution to these is >= 0, so more history can only push the total up or leave it
unchanged). sum_arrival_delay/sum_departure_delay/sum_final_delay are signed (a very
early train contributes a negative number), so an older, purged trip could make the
FULL total lower than the recomputed subset even with perfectly correct code -- those
are reported for visibility but never flagged as a hard mismatch.

Usage:
    python verify_aggregate.py --db tchoutchou.db
    python verify_aggregate.py --db tchoutchou.db --max-report 50     # cap printed mismatches
    python verify_aggregate.py --db tchoutchou.db --trains 9575,6683  # narrow scope, e.g.
                                                                       # for a faster focused re-check
Exit code is non-zero if any hard mismatch was found -- safe to wire into the same daily
.bat/Task Scheduler job as aggregate.py/purge_raw.py and alert on failure via the
redirected log, same pattern already used for those two.
"""

import argparse
import re
import sqlite3
from datetime import datetime
from collections import defaultdict

ON_TIME_THRESHOLD_SEC = 5 * 60
LATE_5_THRESHOLD_SEC = 5 * 60
LATE_15_THRESHOLD_SEC = 15 * 60
LATE_30_THRESHOLD_SEC = 30 * 60

_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")

# Column layouts, matching each table's real schema order. MONOTONIC marks which
# indices can only ever go up as more history is folded in (see module docstring) --
# those get a hard actual>=expected assertion. Everything else is printed for
# visibility only.
TSS_COLUMNS = ["observations", "arrival_observations", "sum_arrival_delay", "sum_arrival_delay_sq",
               "departure_observations", "sum_departure_delay", "sum_departure_delay_sq",
               "on_time_count", "late_5_count", "late_15_count", "late_30_count"]
TSS_MONOTONIC = [0, 1, 3, 4, 6, 7, 8, 9, 10]

STATION_COLUMNS = ["observations", "sum_arrival_delay", "sum_arrival_delay_sq",
                    "on_time_count", "late_5_count", "late_15_count", "late_30_count"]
STATION_MONOTONIC = [0, 2, 3, 4, 5, 6]

TRAIN_COLUMNS = ["observations", "sum_final_delay", "sum_final_delay_sq",
                  "on_time_count", "late_5_count", "late_15_count", "late_30_count"]
TRAIN_MONOTONIC = [0, 2, 3, 4, 5, 6]


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


def recompute_final_by_stop(conn, trip_id, start_date):
    """Independent re-derivation of 'the final observation per stop', via a SQL window
    function with an explicit tiebreak -- a different mechanism than aggregate.py's
    ascending-order dict-overwrite loop, on purpose (see module docstring)."""
    rows = conn.execute(
        "SELECT stop_id, arrival_delay, arrival_time, departure_delay, departure_time FROM ("
        "  SELECT stu.stop_id, stu.arrival_delay, stu.arrival_time, stu.departure_delay, stu.departure_time, "
        "         ROW_NUMBER() OVER ("
        "           PARTITION BY stu.stop_id "
        "           ORDER BY s.fetched_at_utc DESC, s.id DESC"
        "         ) AS rn "
        "  FROM stop_time_updates stu "
        "  JOIN trip_updates tu ON tu.id = stu.trip_update_id "
        "  JOIN snapshots s ON s.id = tu.snapshot_id "
        "  WHERE tu.trip_id = ? AND tu.start_date = ?"
        ") WHERE rn = 1",
        (trip_id, start_date),
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def recompute_trip_identity(conn, trip_id, start_date):
    return conn.execute(
        "SELECT direction_id, train_type FROM trip_updates tu JOIN snapshots s ON s.id = tu.snapshot_id "
        "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc DESC, s.id DESC LIMIT 1",
        (trip_id, start_date),
    ).fetchone()


def recompute_expected(conn, checkable_trips):
    expected_tss = defaultdict(lambda: [0] * len(TSS_COLUMNS))
    expected_station = defaultdict(lambda: [0] * len(STATION_COLUMNS))
    expected_train = defaultdict(lambda: [0] * len(TRAIN_COLUMNS))
    checked = 0
    skipped_no_stops = 0

    for trip_id, start_date, train_number in checkable_trips:
        identity = recompute_trip_identity(conn, trip_id, start_date)
        if identity is None:
            continue
        direction_id, train_type = identity
        direction_id_val = direction_id if direction_id is not None else -1
        train_type_bucket = train_type or "unknown"
        day_type = day_type_for(start_date)

        final_by_stop = recompute_final_by_stop(conn, trip_id, start_date)
        if not final_by_stop:
            skipped_no_stops += 1
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

            e = expected_tss[(train_number, station_uic, direction_id_val, day_type)]
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

            se = expected_station[(station_uic, train_type_bucket, day_type)]
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
        te = expected_train[(train_number, day_type)]
        te[0] += 1
        te[1] += trip_final_delay or 0
        te[2] += (trip_final_delay ** 2 if trip_final_delay is not None else 0)
        te[3] += ot
        te[4] += l5
        te[5] += l15
        te[6] += l30

    print(f"\nChecked {checked} trip(s) with usable raw data ({skipped_no_stops} had no "
          f"resolvable stops -- consistent with aggregate.py's own handling, not a bug).")
    return expected_tss, expected_station, expected_train


def diff_table(conn, table, key_cols, key_sql_cols, columns, monotonic_idx, expected_dict, max_report):
    mismatches = []
    informational = []
    for key, expected in expected_dict.items():
        where = " AND ".join(f"{c}=?" for c in key_sql_cols)
        actual = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", key
        ).fetchone()
        if actual is None:
            mismatches.append((key, "MISSING entirely", expected, None))
            continue
        bad = [(columns[i], expected[i], actual[i]) for i in monotonic_idx if actual[i] < expected[i]]
        if bad:
            mismatches.append((key, bad, expected, actual))
        signed_idx = [i for i in range(len(columns)) if i not in monotonic_idx]
        for i in signed_idx:
            if actual[i] != expected[i]:
                informational.append((key, columns[i], expected[i], actual[i]))

    print(f"\n{table}: {len(expected_dict)} key(s) recomputed, {len(mismatches)} hard mismatch(es).")
    for key, bad, expected, actual in mismatches[:max_report]:
        print(f"  key={dict(zip(key_cols, key))}")
        if bad == "MISSING entirely":
            print(f"    expected (recomputed from raw subset): {dict(zip(columns, expected))}")
            print(f"    actual: NO ROW FOUND -- should exist, this trip was supposedly aggregated")
        else:
            for col, exp_val, act_val in bad:
                print(f"    {col}: actual={act_val} < recomputed(subset)={exp_val}  <-- should never happen if correct")
    if len(mismatches) > max_report:
        print(f"  ... and {len(mismatches) - max_report} more (raise --max-report to see them)")
    if informational:
        print(f"  ({len(informational)} signed-sum column difference(s), not flagged -- see docstring on why)")
    return mismatches


def check_cancelled_flags(conn, checkable_trips, max_report):
    mismatches = []
    for trip_id, start_date, train_number in checkable_trips:
        rows = conn.execute(
            "SELECT schedule_relationship FROM trip_updates WHERE trip_id=? AND start_date=?",
            (trip_id, start_date),
        ).fetchall()
        recomputed_cancelled = any((r[0] or "").upper().startswith("CANCEL") for r in rows)
        actual = conn.execute(
            "SELECT cancelled FROM aggregation_state WHERE trip_id=? AND start_date=?",
            (trip_id, start_date),
        ).fetchone()
        if actual is not None and bool(actual[0]) != recomputed_cancelled:
            mismatches.append((trip_id, start_date, recomputed_cancelled, bool(actual[0])))
    print(f"\nCancelled-flag check: {len(mismatches)} mismatch(es) out of {len(checkable_trips)} trip(s).")
    for trip_id, start_date, recomputed, actual in mismatches[:max_report]:
        print(f"  trip_id={trip_id} start_date={start_date}: recomputed cancelled={recomputed}, stored={actual}")
    return mismatches


def check_trip_counts(conn, max_report):
    """Doesn't need raw data at all -- checks aggregation_state vs train_stats for
    every train across FULL history, not just the retention-window subset. Exact
    equality is the right check here (not >=), since neither side is limited to a
    subset -- both cover everything ever aggregated. Catches double-counting or
    dropped trips anywhere in history, not just recently."""
    rows = conn.execute(
        "SELECT train_number, "
        "  SUM(CASE WHEN cancelled=0 THEN 1 ELSE 0 END) AS agg_observed, "
        "  SUM(CASE WHEN cancelled=1 THEN 1 ELSE 0 END) AS agg_cancelled "
        "FROM aggregation_state WHERE train_number IS NOT NULL GROUP BY train_number"
    ).fetchall()
    mismatches = []
    for train_number, agg_observed, agg_cancelled in rows:
        totals = conn.execute(
            "SELECT COALESCE(SUM(observations),0), COALESCE(SUM(cancelled_count),0) "
            "FROM train_stats WHERE train_number=?",
            (train_number,),
        ).fetchone()
        stats_observed, stats_cancelled = totals
        if stats_observed != agg_observed or stats_cancelled != agg_cancelled:
            mismatches.append((train_number, agg_observed, agg_cancelled, stats_observed, stats_cancelled))
    print(f"\nTrip-count coverage check (full history, no retention limit): "
          f"{len(mismatches)} train(s) out of {len(rows)} with a mismatch.")
    for train_number, ao, ac, so, sc in mismatches[:max_report]:
        print(f"  train {train_number}: aggregation_state says {ao} observed + {ac} cancelled, "
              f"train_stats sums to {so} observed + {sc} cancelled")
    return mismatches


def check_route_variant_ties(conn, max_report):
    """Informational, not necessarily a bug: if the top route variants for a train tie
    on observed_count, most_common_origin/destination_uic in the trains table can flip
    between aggregate.py runs without any real data change, since SQLite doesn't
    guarantee tie-break order without an explicit secondary sort key."""
    rows = conn.execute(
        "SELECT train_number, observed_count, COUNT(*) FROM train_route_variants trv "
        "WHERE observed_count = (SELECT MAX(observed_count) FROM train_route_variants t2 "
        "  WHERE t2.train_number = trv.train_number) "
        "GROUP BY train_number, observed_count HAVING COUNT(*) > 1"
    ).fetchall()
    print(f"\nRoute-variant tie check (informational, not counted as a mismatch): "
          f"{len(rows)} train(s) with a tied 'most common route'.")
    for train_number, count, n in rows[:max_report]:
        print(f"  train {train_number}: {n} variants tied at observed_count={count}")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--max-report", type=int, default=25, help="Max mismatches to print per check")
    ap.add_argument("--trains", default=None, help="Comma-separated train_numbers to limit the run to")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

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

    print(f"=== verify_aggregate.py against {args.db} ===")
    print(f"{len(checkable_trips)} aggregated trip(s) still have raw data available to check against.")
    if not checkable_trips:
        print("Nothing to check right now -- either nothing's been aggregated yet, or "
              "everything aggregated so far has already had its raw data purged. Run this "
              "right after aggregate.py, before purge_raw.py, to always have something to check.")

    all_mismatches = []
    if checkable_trips:
        expected_tss, expected_station, expected_train = recompute_expected(conn, checkable_trips)
        all_mismatches += diff_table(
            conn, "train_station_stats",
            ["train_number", "station_uic", "direction_id", "day_type"],
            ["train_number", "station_uic", "direction_id", "day_type"],
            TSS_COLUMNS, TSS_MONOTONIC, expected_tss, args.max_report,
        )
        all_mismatches += diff_table(
            conn, "station_stats",
            ["station_uic", "train_type", "day_type"],
            ["station_uic", "train_type", "day_type"],
            STATION_COLUMNS, STATION_MONOTONIC, expected_station, args.max_report,
        )
        all_mismatches += diff_table(
            conn, "train_stats",
            ["train_number", "day_type"],
            ["train_number", "day_type"],
            TRAIN_COLUMNS, TRAIN_MONOTONIC, expected_train, args.max_report,
        )
        all_mismatches += check_cancelled_flags(conn, checkable_trips, args.max_report)

    all_mismatches += check_trip_counts(conn, args.max_report)
    check_route_variant_ties(conn, args.max_report)

    conn.close()

    print(f"\n=== Summary: {len(all_mismatches)} hard mismatch(es) found "
          f"(route-variant ties are informational, not counted) ===")
    return 1 if all_mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
