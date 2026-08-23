#!/usr/bin/env python3
"""
Folds "completed" trips out of the raw layer into the permanent stats tables
(trains, train_station_stats, train_stats, station_stats, train_route_variants).

A trip is "completed" once its start_date is at least --min-age-days in the past
(default 1) -- by then SNCF's real-time feed has stopped tracking it, so whatever we
last observed for each of its stops is as final as it's going to get. "Final" means:
for each stop_id this trip ever reported, take the arrival/departure delay from the
LATEST snapshot that still had that stop in its stop_time_update list -- not the
first-seen prediction, which can be hours out of date.

Idempotent: already-processed (trip_id, start_date) pairs are skipped (tracked in
aggregation_state), so it's safe to run this daily (e.g. via a scheduled task) without
double-counting. Run this BEFORE purge_raw.py -- purge_raw.py refuses to delete raw
data for a trip that hasn't been aggregated yet.

What's NOT computed here (see README's "Two-layer architecture" section):
- Exact percentiles (P90/P95) -- these tables keep sums/sum-of-squares, not full
  histograms, so you get mean/variance directly but need the raw layer (while it's
  still within the retention window) for exact quantiles.
- True cancellation detection (a trip that never appeared in the feed at all) --
  cancelled_count only counts trips the feed explicitly marked CANCELED. Detecting a
  no-show trip needs cross-referencing the static schedule's calendar, which isn't
  wired up yet.
- Delay propagation (given +10 min at station A, what's the expected delay at station
  B) -- a natural next table once train_station_stats has enough history to be useful;
  deliberately left out of this first pass to keep the aggregation logic reviewable.

Usage:
    python aggregate.py --db tchoutchou.db
    python aggregate.py --db tchoutchou.db --min-age-days 2
"""

import argparse
import re
import sqlite3
from datetime import datetime, timedelta, timezone

ON_TIME_THRESHOLD_SEC = 5 * 60
LATE_5_THRESHOLD_SEC = 5 * 60
LATE_15_THRESHOLD_SEC = 15 * 60
LATE_30_THRESHOLD_SEC = 30 * 60

_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")


def extract_uic(stop_id):
    if not stop_id:
        return None
    m = _STOP_UIC_RE.search(stop_id)
    return m.group(1) if m else None


def day_type_for(start_date: str) -> str:
    """start_date is GTFS YYYYMMDD. Returns 'weekday' | 'saturday' | 'sunday'.

    Deliberately 3 buckets, not 7 (or month/season on top) -- enough to catch the
    weekday-vs-weekend split that actually matters for French rail scheduling/crowding,
    without splitting a train's history into dozens of near-empty buckets. Finer slicing
    is always possible later by recomputing from the raw layer while it's retained.
    """
    d = datetime.strptime(start_date, "%Y%m%d").date()
    wd = d.weekday()  # 0=Mon..6=Sun
    if wd == 5:
        return "saturday"
    if wd == 6:
        return "sunday"
    return "weekday"


def bucket_counts(delay_seconds):
    """Returns (on_time, late_5, late_15, late_30) increments for one observation."""
    if delay_seconds is None:
        return 0, 0, 0, 0
    on_time = 1 if delay_seconds < ON_TIME_THRESHOLD_SEC else 0
    late_5 = 1 if delay_seconds >= LATE_5_THRESHOLD_SEC else 0
    late_15 = 1 if delay_seconds >= LATE_15_THRESHOLD_SEC else 0
    late_30 = 1 if delay_seconds >= LATE_30_THRESHOLD_SEC else 0
    return on_time, late_5, late_15, late_30


def find_candidate_trips(conn, min_age_days):
    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=min_age_days)).strftime("%Y%m%d")
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT tu.trip_id, tu.start_date FROM trip_updates tu "
        "LEFT JOIN aggregation_state ags "
        "  ON ags.trip_id = tu.trip_id AND ags.start_date = tu.start_date "
        "WHERE tu.start_date IS NOT NULL AND tu.start_date < ? AND ags.trip_id IS NULL",
        (cutoff_date,),
    )
    return cur.fetchall()


def process_trip(cur, trip_id, start_date):
    now = datetime.now(timezone.utc).isoformat()
    day_type = day_type_for(start_date)

    cur.execute(
        "SELECT tu.commercial_train_number, tu.service_code, tu.train_type, tu.origin_uic, "
        "tu.destination_uic, tu.direction_id, tu.schedule_relationship "
        "FROM trip_updates tu JOIN snapshots s ON s.id = tu.snapshot_id "
        "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc ASC",
        (trip_id, start_date),
    )
    tu_rows = cur.fetchall()
    if not tu_rows:
        return  # shouldn't happen (candidate came from this same table), but be safe

    train_number, service_code, train_type, origin_uic, destination_uic, direction_id, _sr = tu_rows[-1]
    cancelled = any((row[6] or "").upper().startswith("CANCEL") for row in tu_rows)

    if train_number is None:
        # Can't attribute anything meaningful (train number extraction failed for every
        # poll of this trip) -- mark processed anyway so we don't retry it forever.
        cur.execute(
            "INSERT INTO aggregation_state (trip_id, start_date, train_number, cancelled, aggregated_at_utc) "
            "VALUES (?, ?, ?, ?, ?)",
            (trip_id, start_date, None, int(cancelled), now),
        )
        return

    # --- train identity + route variant (always recorded, cancelled or not) ---
    cur.execute(
        "INSERT INTO train_route_variants (train_number, origin_uic, destination_uic, observed_count) "
        "VALUES (?, ?, ?, 1) "
        "ON CONFLICT(train_number, origin_uic, destination_uic) DO UPDATE SET "
        "observed_count = observed_count + 1",
        (train_number, origin_uic, destination_uic),
    )
    cur.execute(
        "SELECT origin_uic, destination_uic FROM train_route_variants "
        "WHERE train_number=? ORDER BY observed_count DESC LIMIT 1",
        (train_number,),
    )
    top_origin, top_dest = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM train_route_variants WHERE train_number=?", (train_number,))
    variant_count = cur.fetchone()[0]

    cur.execute(
        "INSERT INTO trains (train_number, service_code, train_type, most_common_origin_uic, "
        "most_common_destination_uic, route_variant_count, trips_observed, first_seen_date, "
        "last_seen_date, updated_at_utc) VALUES (?,?,?,?,?,?,1,?,?,?) "
        "ON CONFLICT(train_number) DO UPDATE SET "
        "service_code=excluded.service_code, "
        "train_type=excluded.train_type, "
        "most_common_origin_uic=excluded.most_common_origin_uic, "
        "most_common_destination_uic=excluded.most_common_destination_uic, "
        "route_variant_count=excluded.route_variant_count, "
        "trips_observed=trips_observed+1, "
        "first_seen_date=CASE WHEN trains.first_seen_date IS NULL OR excluded.first_seen_date < trains.first_seen_date "
        "  THEN excluded.first_seen_date ELSE trains.first_seen_date END, "
        "last_seen_date=CASE WHEN trains.last_seen_date IS NULL OR excluded.last_seen_date > trains.last_seen_date "
        "  THEN excluded.last_seen_date ELSE trains.last_seen_date END, "
        "updated_at_utc=excluded.updated_at_utc",
        (train_number, service_code, train_type, top_origin, top_dest, variant_count, start_date, start_date, now),
    )

    if cancelled:
        cur.execute(
            "INSERT INTO train_stats (train_number, day_type, observations, cancelled_count, "
            "sum_final_delay, sum_final_delay_sq, on_time_count, late_5_count, late_15_count, "
            "late_30_count, updated_at_utc) VALUES (?,?,0,1,0,0,0,0,0,0,?) "
            "ON CONFLICT(train_number, day_type) DO UPDATE SET "
            "cancelled_count = cancelled_count + 1, updated_at_utc = excluded.updated_at_utc",
            (train_number, day_type, now),
        )
        cur.execute(
            "INSERT INTO aggregation_state (trip_id, start_date, train_number, cancelled, aggregated_at_utc) "
            "VALUES (?, ?, ?, 1, ?)",
            (trip_id, start_date, train_number, now),
        )
        return

    # --- final observed delay per stop: last snapshot to report that stop wins ---
    #
    # stop_sequence is deliberately NOT selected here (or used below to find the final
    # stop) -- SNCF's live feed never actually populates it. Confirmed by direct count
    # against two real collection runs: 0 non-null stop_sequence values out of 250,000+
    # stop_time_updates rows. The previous version of this function used
    # max(stop_sequence) to find "the final stop of the trip"; since every row ties at
    # the same (missing) value, Python's max() silently returned the first-inserted
    # item instead -- effectively the origin stop, not the destination -- which would
    # have made train_stats.sum_final_delay measure the wrong end of the trip. Using
    # arrival_time/departure_time instead (absolute epoch seconds, always present, and
    # monotonically increasing along the route) avoids depending on a field that isn't
    # actually there.
    cur.execute(
        "SELECT stu.stop_id, stu.arrival_delay, stu.arrival_time, stu.departure_delay, stu.departure_time "
        "FROM stop_time_updates stu "
        "JOIN trip_updates tu ON tu.id = stu.trip_update_id "
        "JOIN snapshots s ON s.id = tu.snapshot_id "
        "WHERE tu.trip_id=? AND tu.start_date=? ORDER BY s.fetched_at_utc ASC",
        (trip_id, start_date),
    )
    final_by_stop = {}
    for stop_id, arrival_delay, arrival_time, departure_delay, departure_time in cur.fetchall():
        final_by_stop[stop_id] = (arrival_delay, arrival_time, departure_delay, departure_time)

    if not final_by_stop:
        cur.execute(
            "INSERT INTO aggregation_state (trip_id, start_date, train_number, cancelled, aggregated_at_utc) "
            "VALUES (?, ?, ?, 0, ?)",
            (trip_id, start_date, train_number, now),
        )
        return

    direction_id_val = direction_id if direction_id is not None else -1
    train_type_bucket = train_type or "unknown"

    for stop_id, (arrival_delay, _arrival_time, departure_delay, _departure_time) in final_by_stop.items():
        station_uic = extract_uic(stop_id)
        if station_uic is None:
            continue
        on_time, late5, late15, late30 = bucket_counts(arrival_delay)
        arr_present = 1 if arrival_delay is not None else 0
        dep_present = 1 if departure_delay is not None else 0

        cur.execute(
            "INSERT INTO train_station_stats (train_number, station_uic, direction_id, day_type, "
            "observations, arrival_observations, sum_arrival_delay, sum_arrival_delay_sq, "
            "departure_observations, sum_departure_delay, sum_departure_delay_sq, "
            "on_time_count, late_5_count, late_15_count, late_30_count, updated_at_utc) "
            "VALUES (?,?,?,?, 1, ?,?,?, ?,?,?, ?,?,?,?, ?) "
            "ON CONFLICT(train_number, station_uic, direction_id, day_type) DO UPDATE SET "
            "observations = observations + 1, "
            "arrival_observations = arrival_observations + excluded.arrival_observations, "
            "sum_arrival_delay = sum_arrival_delay + excluded.sum_arrival_delay, "
            "sum_arrival_delay_sq = sum_arrival_delay_sq + excluded.sum_arrival_delay_sq, "
            "departure_observations = departure_observations + excluded.departure_observations, "
            "sum_departure_delay = sum_departure_delay + excluded.sum_departure_delay, "
            "sum_departure_delay_sq = sum_departure_delay_sq + excluded.sum_departure_delay_sq, "
            "on_time_count = on_time_count + excluded.on_time_count, "
            "late_5_count = late_5_count + excluded.late_5_count, "
            "late_15_count = late_15_count + excluded.late_15_count, "
            "late_30_count = late_30_count + excluded.late_30_count, "
            "updated_at_utc = excluded.updated_at_utc",
            (
                train_number, station_uic, direction_id_val, day_type,
                arr_present, arrival_delay or 0, (arrival_delay ** 2 if arrival_delay is not None else 0),
                dep_present, departure_delay or 0, (departure_delay ** 2 if departure_delay is not None else 0),
                on_time, late5, late15, late30, now,
            ),
        )

        cur.execute(
            "INSERT INTO station_stats (station_uic, train_type, day_type, observations, "
            "sum_arrival_delay, sum_arrival_delay_sq, on_time_count, late_5_count, late_15_count, "
            "late_30_count, updated_at_utc) VALUES (?,?,?, ?,?,?, ?,?,?,?, ?) "
            "ON CONFLICT(station_uic, train_type, day_type) DO UPDATE SET "
            "observations = observations + excluded.observations, "
            "sum_arrival_delay = sum_arrival_delay + excluded.sum_arrival_delay, "
            "sum_arrival_delay_sq = sum_arrival_delay_sq + excluded.sum_arrival_delay_sq, "
            "on_time_count = on_time_count + excluded.on_time_count, "
            "late_5_count = late_5_count + excluded.late_5_count, "
            "late_15_count = late_15_count + excluded.late_15_count, "
            "late_30_count = late_30_count + excluded.late_30_count, "
            "updated_at_utc = excluded.updated_at_utc",
            (
                station_uic, train_type_bucket, day_type,
                arr_present, arrival_delay or 0, (arrival_delay ** 2 if arrival_delay is not None else 0),
                on_time, late5, late15, late30, now,
            ),
        )

    # --- end-to-end trip delay: the final stop, identified by whichever stop has the
    # latest observed arrival/departure time -- see the comment above on why this isn't
    # stop_sequence. ---
    def _stop_order_time(v):
        _arrival_delay, arrival_time, _departure_delay, departure_time = v
        t = arrival_time if arrival_time is not None else departure_time
        return t if t is not None else -1

    _last_stop_id, (final_arrival_delay, _final_arrival_time, final_departure_delay, _final_departure_time) = max(
        final_by_stop.items(), key=lambda kv: _stop_order_time(kv[1])
    )
    trip_final_delay = final_arrival_delay if final_arrival_delay is not None else final_departure_delay
    on_time, late5, late15, late30 = bucket_counts(trip_final_delay)

    cur.execute(
        "INSERT INTO train_stats (train_number, day_type, observations, cancelled_count, "
        "sum_final_delay, sum_final_delay_sq, on_time_count, late_5_count, late_15_count, "
        "late_30_count, updated_at_utc) VALUES (?,?, 1, 0, ?,?, ?,?,?,?, ?) "
        "ON CONFLICT(train_number, day_type) DO UPDATE SET "
        "observations = observations + 1, "
        "sum_final_delay = sum_final_delay + excluded.sum_final_delay, "
        "sum_final_delay_sq = sum_final_delay_sq + excluded.sum_final_delay_sq, "
        "on_time_count = on_time_count + excluded.on_time_count, "
        "late_5_count = late_5_count + excluded.late_5_count, "
        "late_15_count = late_15_count + excluded.late_15_count, "
        "late_30_count = late_30_count + excluded.late_30_count, "
        "updated_at_utc = excluded.updated_at_utc",
        (
            train_number, day_type,
            trip_final_delay or 0, (trip_final_delay ** 2 if trip_final_delay is not None else 0),
            on_time, late5, late15, late30, now,
        ),
    )

    cur.execute(
        "INSERT INTO aggregation_state (trip_id, start_date, train_number, cancelled, aggregated_at_utc) "
        "VALUES (?, ?, ?, 0, ?)",
        (trip_id, start_date, train_number, now),
    )


def find_platform_candidate_trips(conn, min_age_days):
    """
    SIRI's identity key is (train_number, calendar_date), not (trip_id, start_date) --
    see db.py's "PLATFORM LAYER" section. calendar_date is best-effort (derived from
    OriginAimedDepartureTime by siri_parse.calendar_date_from_iso), not a true SIRI
    field, but it's the practical stand-in for "which service day is this".
    """
    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=min_age_days)).strftime("%Y%m%d")
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT pj.train_number, pj.calendar_date FROM platform_journeys pj "
        "LEFT JOIN platform_aggregation_state pas "
        "  ON pas.train_number = pj.train_number AND pas.calendar_date = pj.calendar_date "
        "WHERE pj.train_number IS NOT NULL AND pj.calendar_date IS NOT NULL "
        "  AND pj.calendar_date < ? AND pas.train_number IS NULL",
        (cutoff_date,),
    )
    return cur.fetchall()


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def process_platform_trip(cur, train_number, calendar_date):
    """
    Folds one (train_number, calendar_date) journey's platform_calls into the permanent
    platform_variants ("what platform does this train usually use here" -- the Likely
    tier) and platform_lead_time_stats ("how far ahead does SNCF usually confirm it")
    tables.

    platform_calls now holds only the current/final state per stop, UPSERTed at ingest
    time rather than one row per poll (see db.py's PLATFORM LAYER comment and
    ingest.py's poll_siri_feed()) -- so this is a direct read of one row per stop, not a
    scan over snapshot history the way this function used to work (and the way
    process_trip()'s GTFS-RT equivalent still does, since that side wasn't changed).
    The *_platform_first_confirmed_at_utc columns carry the one piece of history lead
    time needs, captured once at ingest time.

    Only call_type='recorded' counts as confirmed -- an estimated platform (if SIRI
    ever populates one) is still a guess and would pollute the historical signal.
    """
    now = datetime.now(timezone.utc).isoformat()

    cur.execute(
        "SELECT stop_point_ref, call_type, aimed_arrival_time, arrival_platform_name, "
        "arrival_platform_first_confirmed_at_utc, aimed_departure_time, departure_platform_name, "
        "departure_platform_first_confirmed_at_utc "
        "FROM platform_calls WHERE train_number=? AND calendar_date=?",
        (train_number, calendar_date),
    )
    rows = cur.fetchall()
    if not rows:
        cur.execute(
            "INSERT INTO platform_aggregation_state (train_number, calendar_date, aggregated_at_utc) "
            "VALUES (?, ?, ?)",
            (train_number, calendar_date, now),
        )
        return

    for (stop_ref, call_type, aimed_arr, arr_platform, arr_confirmed_ts,
         aimed_dep, dep_platform, dep_confirmed_ts) in rows:
        for field, aimed_time, platform, confirmed_ts in (
            ("arrival", aimed_arr, arr_platform, arr_confirmed_ts),
            ("departure", aimed_dep, dep_platform, dep_confirmed_ts),
        ):
            if aimed_time is None:
                continue  # this stop doesn't have this call type at all (e.g. no arrival at the origin)

            if call_type == "recorded" and platform:
                cur.execute(
                    "INSERT INTO platform_variants (train_number, stop_point_ref, call_field, "
                    "platform_name, observed_count, last_observed_date) VALUES (?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(train_number, stop_point_ref, call_field, platform_name) DO UPDATE SET "
                    "observed_count = observed_count + 1, last_observed_date = excluded.last_observed_date",
                    (train_number, stop_ref, field, platform, calendar_date),
                )

                aimed_dt = _parse_iso(aimed_time)
                confirmed_dt = _parse_iso(confirmed_ts)
                lead_time_seconds = None
                if aimed_dt is not None and confirmed_dt is not None:
                    lead_time_seconds = (aimed_dt - confirmed_dt).total_seconds()

                cur.execute(
                    "INSERT INTO platform_lead_time_stats (train_number, stop_point_ref, call_field, "
                    "observations, sum_lead_time_seconds, sum_lead_time_seconds_sq, never_confirmed_count, "
                    "updated_at_utc) VALUES (?, ?, ?, ?, ?, ?, 0, ?) "
                    "ON CONFLICT(train_number, stop_point_ref, call_field) DO UPDATE SET "
                    "observations = observations + excluded.observations, "
                    "sum_lead_time_seconds = sum_lead_time_seconds + excluded.sum_lead_time_seconds, "
                    "sum_lead_time_seconds_sq = sum_lead_time_seconds_sq + excluded.sum_lead_time_seconds_sq, "
                    "updated_at_utc = excluded.updated_at_utc",
                    (
                        train_number, stop_ref, field,
                        1 if lead_time_seconds is not None else 0,
                        lead_time_seconds or 0, (lead_time_seconds ** 2 if lead_time_seconds is not None else 0),
                        now,
                    ),
                )
            else:
                # Ran (we saw estimated calls for it) but SNCF never confirmed a platform.
                cur.execute(
                    "INSERT INTO platform_lead_time_stats (train_number, stop_point_ref, call_field, "
                    "observations, sum_lead_time_seconds, sum_lead_time_seconds_sq, never_confirmed_count, "
                    "updated_at_utc) VALUES (?, ?, ?, 0, 0, 0, 1, ?) "
                    "ON CONFLICT(train_number, stop_point_ref, call_field) DO UPDATE SET "
                    "never_confirmed_count = never_confirmed_count + 1, updated_at_utc = excluded.updated_at_utc",
                    (train_number, stop_ref, field, now),
                )

    cur.execute(
        "INSERT INTO platform_aggregation_state (train_number, calendar_date, aggregated_at_utc) "
        "VALUES (?, ?, ?)",
        (train_number, calendar_date, now),
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--min-age-days", type=int, default=1,
                     help="Only aggregate trips whose start_date is at least this many days in the past")
    ap.add_argument("--commit-every", type=int, default=200)
    args = ap.parse_args()

    # timeout=30 matches db.py's connect() (used by ingest.py) -- this script previously
    # used sqlite3's 5-second default, which is far too short given ingest.py's writer
    # is live the whole time this runs (tens of minutes to hours over tens of thousands
    # of trips). Under the old default, a single lock-contention hiccup raised
    # sqlite3.OperationalError: database is locked and crashed the ENTIRE run,
    # silently rolling back every trip processed since the last commit (2026-08-22
    # incident: crashed after just 200/22544 trips). 30s gives SQLite's own busy-retry
    # loop room to wait out ingest.py's brief writes instead of giving up immediately.
    conn = sqlite3.connect(args.db, timeout=30)
    cur = conn.cursor()

    candidates = find_candidate_trips(conn, args.min_age_days)
    print(f"{len(candidates)} trip(s) ready to aggregate "
          f"(start_date more than {args.min_age_days} day(s) old, not yet processed).")

    # Each trip's work is wrapped in its own SAVEPOINT so a failure on ONE trip (a
    # lock-timeout that still exceeds 30s, a data anomaly, anything unexpected) rolls
    # back just that trip's partial inserts and moves on, instead of raising an
    # uncaught exception that kills the whole run and silently discards every trip
    # processed since the last commit. A skipped trip never reaches its
    # aggregation_state INSERT (that's always the last statement in process_trip()), so
    # it's correctly retried on the next run rather than lost or half-recorded.
    processed = 0
    errors = 0
    for trip_id, start_date in candidates:
        cur.execute("SAVEPOINT trip_sp")
        try:
            process_trip(cur, trip_id, start_date)
        except Exception as exc:
            cur.execute("ROLLBACK TO trip_sp")
            cur.execute("RELEASE trip_sp")
            errors += 1
            print(f"  ERROR processing trip_id={trip_id} start_date={start_date}: {exc} "
                  f"-- skipped, will retry next run")
            continue
        cur.execute("RELEASE trip_sp")
        processed += 1
        if processed % args.commit_every == 0:
            conn.commit()
            print(f"  ...{processed}/{len(candidates)}")
    conn.commit()
    print(f"Done. Aggregated {processed} trip(s)."
          + (f" {errors} trip(s) hit an error and were skipped for retry next run." if errors else ""))

    platform_candidates = find_platform_candidate_trips(conn, args.min_age_days)
    print(f"{len(platform_candidates)} platform journey(s) ready to aggregate.")

    platform_processed = 0
    platform_errors = 0
    for train_number, calendar_date in platform_candidates:
        cur.execute("SAVEPOINT trip_sp")
        try:
            process_platform_trip(cur, train_number, calendar_date)
        except Exception as exc:
            cur.execute("ROLLBACK TO trip_sp")
            cur.execute("RELEASE trip_sp")
            platform_errors += 1
            print(f"  ERROR processing train_number={train_number} calendar_date={calendar_date}: {exc} "
                  f"-- skipped, will retry next run")
            continue
        cur.execute("RELEASE trip_sp")
        platform_processed += 1
        if platform_processed % args.commit_every == 0:
            conn.commit()
            print(f"  ...{platform_processed}/{len(platform_candidates)}")
    conn.commit()
    print(f"Done. Aggregated {platform_processed} platform journey(s)."
          + (f" {platform_errors} journey(s) hit an error and were skipped for retry next run." if platform_errors else ""))

    conn.close()


if __name__ == "__main__":
    main()
