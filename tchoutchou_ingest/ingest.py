#!/usr/bin/env python3
"""
TchouTchou feasibility data collector.

Polls SNCF's national GTFS-RT feeds (trip_updates + service_alerts) via the
transport.data.gouv.fr proxy, on the cadence SNCF itself refreshes them (~2 min),
and logs every snapshot -- raw and parsed -- to a local SQLite database.

Also polls SNCF's SIRI ET Lite feed (siri_et) -- a separate XML protocol (not GTFS-RT)
that carries SNCF's actual confirmed platform assignments, something the GTFS-RT feeds
never include at all. See siri_parse.py and db.py's "PLATFORM LAYER" section.

Usage:
    python ingest.py                          # run forever, all three feeds, every 120s
    python ingest.py --once                   # single poll of all feeds, then exit (smoke test)
    python ingest.py --duration-hours 48      # run for 48h then stop cleanly (the feasibility run)
    python ingest.py --feeds trip_updates     # only poll one feed
    python ingest.py --feeds siri_et          # only poll the platform feed
    python ingest.py --no-raw                 # skip storing raw gzip blobs (saves disk, loses safety net)
    python ingest.py --no-station-lookup      # skip resolving UIC codes to station names

Stop anytime with Ctrl+C -- the current poll finishes, the DB connection closes cleanly.

Legal note: SNCF's real-time feeds are published under ODbL with the transport.data.gouv.fr
"Conditions Particulières d'Utilisation". Derived analytics with different temporal/
granularity characteristics than the raw feed (e.g. predictive delay scores, aggregated
reliability stats) qualify as "création produite" and are NOT subject to share-alike
redistribution. Attribution to SNCF + the ODbL license is required wherever the app is
used. See README.md for the full summary and source links.
"""

import argparse
import gzip
import logging
import signal
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import requests
from google.transit import gtfs_realtime_pb2

import db
import parse
import siri_parse
import stations

# protocol distinguishes how poll_cycle() dispatches each feed -- gtfs-rt feeds are
# protobuf (parsed via parse.py), siri feeds are XML (parsed via siri_parse.py). Same
# `snapshots` table (and raw_gzip safety net) is shared across both.
FEEDS = {
    "trip_updates": {
        "url": "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates",
        "protocol": "gtfs-rt",
    },
    "service_alerts": {
        "url": "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts",
        "protocol": "gtfs-rt",
    },
    "siri_et": {
        "url": "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable",
        "protocol": "siri",
    },
}

DEFAULT_INTERVAL_SECONDS = 120  # matches SNCF's own refresh cadence -- polling faster buys nothing
REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

logger = logging.getLogger("tchoutchou.ingest")

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    global _shutdown_requested
    logger.info("Shutdown signal received (%s) -- will stop after the current poll.", signum)
    _shutdown_requested = True


def setup_logging(log_file: str):
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(log_file, maxBytes=20 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def fetch_feed(url: str) -> tuple[bytes, int, int]:
    """Returns (raw_bytes, http_status, duration_ms). Raises on final failure after retries."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        start = time.monotonic()
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            duration_ms = int((time.monotonic() - start) * 1000)
            resp.raise_for_status()
            return resp.content, resp.status_code, duration_ms
        except requests.RequestException as exc:
            last_exc = exc
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "Fetch attempt %d/%d failed for %s after %dms: %s",
                attempt, MAX_RETRIES, url, duration_ms, exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise last_exc


def poll_feed(conn, feed_name: str, url: str, store_raw: bool, resolve_stations: bool = True):
    run_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()

    try:
        raw_bytes, http_status, duration_ms = fetch_feed(url)
    except requests.RequestException as exc:
        cur.execute(
            "INSERT INTO ingestion_log (run_at_utc, feed_name, status, http_status, "
            "entity_count, duration_ms, error_message) VALUES (?, ?, 'error', NULL, NULL, NULL, ?)",
            (run_at, feed_name, str(exc)),
        )
        conn.commit()
        logger.error("Giving up on %s this cycle: %s", feed_name, exc)
        return

    feed = gtfs_realtime_pb2.FeedMessage()
    try:
        feed.ParseFromString(raw_bytes)
    except Exception as exc:  # malformed protobuf -- log raw bytes anyway for postmortem
        logger.error("Failed to parse %s feed as protobuf: %s", feed_name, exc)
        cur.execute(
            "INSERT INTO snapshots (feed_name, fetched_at_utc, http_status, fetch_duration_ms, "
            "content_length_bytes, raw_gzip, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feed_name, run_at, http_status, duration_ms, len(raw_bytes),
             gzip.compress(raw_bytes) if store_raw else None, f"parse error: {exc}"),
        )
        conn.commit()
        return

    entity_count = len(feed.entity)
    cur.execute(
        "INSERT INTO snapshots (feed_name, fetched_at_utc, feed_header_timestamp, "
        "gtfs_realtime_version, entity_count, http_status, fetch_duration_ms, "
        "content_length_bytes, raw_gzip, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            feed_name, run_at,
            feed.header.timestamp if feed.header.HasField("timestamp") else None,
            feed.header.gtfs_realtime_version,
            entity_count, http_status, duration_ms, len(raw_bytes),
            gzip.compress(raw_bytes) if store_raw else None,
        ),
    )
    snapshot_id = cur.lastrowid

    tu_count = 0
    stu_count = 0
    alert_count = 0
    seen_uics = set()

    for entity in feed.entity:
        ent = parse.entity_to_dict(entity)

        if entity.HasField("trip_update"):
            row, stu_rows = parse.trip_update_row(snapshot_id, ent)
            seen_uics.add(row.get("origin_uic"))
            seen_uics.add(row.get("destination_uic"))
            cur.execute(
                "INSERT INTO trip_updates (snapshot_id, entity_id, trip_id, route_id, direction_id, "
                "start_date, start_time, schedule_relationship, commercial_train_number, service_code, "
                "train_type, origin_uic, destination_uic, mission_code, vehicle_id, vehicle_label, "
                "trip_update_timestamp, trip_update_delay, stop_time_update_count) "
                "VALUES (:snapshot_id, :entity_id, :trip_id, :route_id, :direction_id, :start_date, "
                ":start_time, :schedule_relationship, :commercial_train_number, :service_code, "
                ":train_type, :origin_uic, :destination_uic, :mission_code, :vehicle_id, "
                ":vehicle_label, :trip_update_timestamp, :trip_update_delay, :stop_time_update_count)",
                row,
            )
            trip_update_id = cur.lastrowid
            for stu_row in stu_rows:
                stu_row["trip_update_id"] = trip_update_id
                cur.execute(
                    "INSERT INTO stop_time_updates (trip_update_id, stop_sequence, stop_id, "
                    "schedule_relationship, arrival_delay, arrival_time, arrival_uncertainty, "
                    "departure_delay, departure_time, departure_uncertainty) VALUES "
                    "(:trip_update_id, :stop_sequence, :stop_id, :schedule_relationship, :arrival_delay, "
                    ":arrival_time, :arrival_uncertainty, :departure_delay, :departure_time, :departure_uncertainty)",
                    stu_row,
                )
            tu_count += 1
            stu_count += len(stu_rows)

        elif entity.HasField("alert"):
            row = parse.service_alert_row(snapshot_id, ent)
            cur.execute(
                "INSERT INTO service_alerts (snapshot_id, entity_id, cause, effect, header_text, "
                "description_text, active_period_start, active_period_end, informed_entities_json) "
                "VALUES (:snapshot_id, :entity_id, :cause, :effect, :header_text, :description_text, "
                ":active_period_start, :active_period_end, :informed_entities_json)",
                row,
            )
            alert_count += 1

    cur.execute(
        "INSERT INTO ingestion_log (run_at_utc, feed_name, status, http_status, entity_count, "
        "duration_ms, error_message) VALUES (?, ?, 'ok', ?, ?, ?, NULL)",
        (run_at, feed_name, http_status, entity_count, duration_ms),
    )
    conn.commit()

    if resolve_stations and seen_uics:
        # Cache-first: only UIC codes not already in the `stations` table trigger an
        # API call. Runs after the main commit so a slow/failed lookup never blocks
        # getting the actual feed data safely onto disk.
        stations.resolve_missing(conn, seen_uics)

    logger.info(
        "%-15s entities=%-4d trip_updates=%-4d stop_time_updates=%-5d alerts=%-3d (%dms)",
        feed_name, entity_count, tu_count, stu_count, alert_count, duration_ms,
    )


def poll_siri_feed(conn, feed_name: str, url: str, store_raw: bool):
    """
    SIRI ET Lite is XML, not protobuf -- a different parser (siri_parse.py) and a
    different set of raw-layer tables (platform_journeys/platform_calls instead of
    trip_updates/stop_time_updates), but the same snapshots table and the same
    fetch-log-commit shape as poll_feed(), so the two are easy to reason about side by
    side.
    """
    run_at = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()

    try:
        raw_bytes, http_status, duration_ms = fetch_feed(url)
    except requests.RequestException as exc:
        cur.execute(
            "INSERT INTO ingestion_log (run_at_utc, feed_name, status, http_status, "
            "entity_count, duration_ms, error_message) VALUES (?, ?, 'error', NULL, NULL, NULL, ?)",
            (run_at, feed_name, str(exc)),
        )
        conn.commit()
        logger.error("Giving up on %s this cycle: %s", feed_name, exc)
        return

    try:
        root = ET.fromstring(raw_bytes)
        journeys = siri_parse.find_journeys(root)
    except ET.ParseError as exc:  # malformed XML -- log raw bytes anyway for postmortem
        logger.error("Failed to parse %s feed as XML: %s", feed_name, exc)
        cur.execute(
            "INSERT INTO snapshots (feed_name, fetched_at_utc, http_status, fetch_duration_ms, "
            "content_length_bytes, raw_gzip, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feed_name, run_at, http_status, duration_ms, len(raw_bytes),
             gzip.compress(raw_bytes) if store_raw else None, f"parse error: {exc}"),
        )
        conn.commit()
        return

    entity_count = len(journeys)
    cur.execute(
        "INSERT INTO snapshots (feed_name, fetched_at_utc, entity_count, http_status, "
        "fetch_duration_ms, content_length_bytes, raw_gzip, error) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (feed_name, run_at, entity_count, http_status, duration_ms, len(raw_bytes),
         gzip.compress(raw_bytes) if store_raw else None),
    )
    snapshot_id = cur.lastrowid

    # UPSERT onto current/final state, not a fresh row per poll -- see db.py's PLATFORM
    # LAYER comment. Only the current best-known value per (train_number, calendar_date,
    # stop_point_ref) is kept; *_platform_first_confirmed_at_utc is the one piece of
    # history preserved (set once, never overwritten), so platform_lead_time_stats stays
    # computable without needing per-poll snapshots.
    call_count = 0
    skipped_journeys = 0
    for journey_elem in journeys:
        j = siri_parse.parse_journey(journey_elem)
        calendar_date = siri_parse.calendar_date_from_iso(j["origin_aimed_departure_time"])
        if not j["train_number"] or not calendar_date:
            # Can't form the (train_number, calendar_date) identity this schema keys
            # on -- rare (SIRI almost always includes TrainNumberRef), but skip rather
            # than crash the whole poll cycle over one malformed journey.
            skipped_journeys += 1
            continue

        # A stop can show up as both a RecordedCall and an EstimatedCall in the same
        # poll response (e.g. arrival confirmed, departure still estimated) --
        # siri_parse.parse_journey() already orders recorded before estimated per the
        # "calls[0] per stop" comment there, so keep the first (recorded-preferring)
        # entry per stop_point_ref and drop the rest, otherwise the second INSERT ...
        # ON CONFLICT within this same loop would clobber a 'recorded' row with an
        # 'estimated' one from the very same poll.
        calls_by_stop = {}
        for call in j["calls"]:
            ref = call.get("StopPointRef")
            if ref and ref not in calls_by_stop:
                calls_by_stop[ref] = call

        cur.execute(
            "INSERT INTO platform_journeys (train_number, calendar_date, line_name, "
            "origin_name, destination_name, product_category_ref, origin_aimed_departure_time, "
            "destination_aimed_arrival_time, call_count, first_seen_snapshot_id, "
            "last_seen_snapshot_id, last_updated_at_utc) VALUES "
            "(:train_number, :calendar_date, :line_name, :origin_name, :destination_name, "
            ":product_category_ref, :origin_aimed_departure_time, :destination_aimed_arrival_time, "
            ":call_count, :snapshot_id, :snapshot_id, :now) "
            "ON CONFLICT(train_number, calendar_date) DO UPDATE SET "
            "line_name=excluded.line_name, "
            "origin_name=excluded.origin_name, "
            "destination_name=excluded.destination_name, "
            "product_category_ref=excluded.product_category_ref, "
            "origin_aimed_departure_time=excluded.origin_aimed_departure_time, "
            "destination_aimed_arrival_time=excluded.destination_aimed_arrival_time, "
            "call_count=excluded.call_count, "
            "last_seen_snapshot_id=excluded.last_seen_snapshot_id, "
            "last_updated_at_utc=excluded.last_updated_at_utc",
            {
                "snapshot_id": snapshot_id,
                "train_number": j["train_number"],
                "calendar_date": calendar_date,
                "line_name": j["line_name"],
                "origin_name": j["origin_name"],
                "destination_name": j["destination_name"],
                "product_category_ref": j["product_category_ref"],
                "origin_aimed_departure_time": j["origin_aimed_departure_time"],
                "destination_aimed_arrival_time": j["destination_aimed_arrival_time"],
                "call_count": len(calls_by_stop),
                "now": run_at,
            },
        )

        for stop_ref, call in calls_by_stop.items():
            params = {
                **call,
                "train_number": j["train_number"],
                "calendar_date": calendar_date,
                "now": run_at,
            }
            cur.execute(
                "INSERT INTO platform_calls (train_number, calendar_date, stop_point_ref, call_type, "
                "stop_point_name, aimed_arrival_time, expected_arrival_time, arrival_platform_name, "
                "arrival_platform_first_confirmed_at_utc, aimed_departure_time, expected_departure_time, "
                "departure_platform_name, departure_platform_first_confirmed_at_utc, last_updated_at_utc) "
                "VALUES (:train_number, :calendar_date, :StopPointRef, :call_type, :StopPointName, "
                ":AimedArrivalTime, :ExpectedArrivalTime, :ArrivalPlatformName, "
                "CASE WHEN :ArrivalPlatformName IS NOT NULL THEN :now ELSE NULL END, "
                ":AimedDepartureTime, :ExpectedDepartureTime, :DeparturePlatformName, "
                "CASE WHEN :DeparturePlatformName IS NOT NULL THEN :now ELSE NULL END, :now) "
                "ON CONFLICT(train_number, calendar_date, stop_point_ref) DO UPDATE SET "
                "call_type=excluded.call_type, "
                "stop_point_name=COALESCE(excluded.stop_point_name, platform_calls.stop_point_name), "
                "aimed_arrival_time=COALESCE(excluded.aimed_arrival_time, platform_calls.aimed_arrival_time), "
                "expected_arrival_time=excluded.expected_arrival_time, "
                "arrival_platform_name=excluded.arrival_platform_name, "
                "arrival_platform_first_confirmed_at_utc="
                "  COALESCE(platform_calls.arrival_platform_first_confirmed_at_utc, excluded.arrival_platform_first_confirmed_at_utc), "
                "aimed_departure_time=COALESCE(excluded.aimed_departure_time, platform_calls.aimed_departure_time), "
                "expected_departure_time=excluded.expected_departure_time, "
                "departure_platform_name=excluded.departure_platform_name, "
                "departure_platform_first_confirmed_at_utc="
                "  COALESCE(platform_calls.departure_platform_first_confirmed_at_utc, excluded.departure_platform_first_confirmed_at_utc), "
                "last_updated_at_utc=excluded.last_updated_at_utc",
                params,
            )
            call_count += 1

    cur.execute(
        "INSERT INTO ingestion_log (run_at_utc, feed_name, status, http_status, entity_count, "
        "duration_ms, error_message) VALUES (?, ?, 'ok', ?, ?, ?, NULL)",
        (run_at, feed_name, http_status, entity_count, duration_ms),
    )
    conn.commit()

    logger.info(
        "%-15s journeys=%-4d calls=%-5d skipped=%-3d (%dms)",
        feed_name, entity_count, call_count, skipped_journeys, duration_ms,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="tchoutchou.db", help="SQLite database path (default: tchoutchou.db)")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS,
                         help=f"Seconds between poll cycles (default: {DEFAULT_INTERVAL_SECONDS})")
    parser.add_argument("--feeds", default="trip_updates,service_alerts,siri_et",
                         help="Comma-separated subset of: " + ",".join(FEEDS.keys()))
    parser.add_argument("--once", action="store_true", help="Poll each feed once, then exit")
    parser.add_argument("--duration-hours", type=float, default=None,
                         help="Stop automatically after this many hours (e.g. 48 for the feasibility run)")
    # DEFERRED (2026-08-18, explicit user decision): don't turn --no-raw on by default
    # yet, even though it's the single biggest lever left on db size. Keep the raw_gzip
    # blobs until parsing is fully validated against real data -- they're what let you
    # re-diagnose a parsing bug (like the stop_sequence one fixed 2026-08-17) after the
    # fact, without needing to re-collect. Revisit turning this on once parsing has
    # proven stable for a while and that safety net stops earning its disk cost.
    parser.add_argument("--no-raw", action="store_true",
                         help="Don't store raw gzip blobs (saves disk, but loses the full-fidelity safety net)")
    parser.add_argument("--no-station-lookup", action="store_true",
                         help="Don't resolve UIC codes to station names via the SNCF reference API")
    parser.add_argument("--log-file", default="tchoutchou_ingest.log")
    args = parser.parse_args()

    setup_logging(args.log_file)

    feed_names = [f.strip() for f in args.feeds.split(",") if f.strip()]
    unknown = set(feed_names) - set(FEEDS.keys())
    if unknown:
        parser.error(f"Unknown feed(s): {unknown}. Choose from {list(FEEDS.keys())}")

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    conn = db.connect(args.db)
    logger.info("Connected to %s. Polling feeds: %s every %ds. store_raw=%s station_lookup=%s",
                args.db, feed_names, args.interval, not args.no_raw, not args.no_station_lookup)

    deadline = None
    if args.duration_hours is not None:
        deadline = time.monotonic() + args.duration_hours * 3600
        logger.info("Will run for %.1f hours then stop.", args.duration_hours)

    try:
        while True:
            cycle_start = time.monotonic()
            for feed_name in feed_names:
                if _shutdown_requested:
                    break
                spec = FEEDS[feed_name]
                if spec["protocol"] == "siri":
                    poll_siri_feed(conn, feed_name, spec["url"], store_raw=not args.no_raw)
                else:
                    poll_feed(conn, feed_name, spec["url"], store_raw=not args.no_raw,
                              resolve_stations=not args.no_station_lookup)

            if args.once or _shutdown_requested:
                break
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("Duration limit reached -- stopping.")
                break

            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, args.interval - elapsed)
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
            time.sleep(sleep_for)
    finally:
        conn.close()
        logger.info("Database connection closed. Exiting.")


if __name__ == "__main__":
    main()
