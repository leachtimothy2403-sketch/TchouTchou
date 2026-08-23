"""
SQLite schema and connection helpers for the TchouTchou feasibility data collector.

Two-layer design:
- RAW LAYER (snapshots, trip_updates, stop_time_updates, service_alerts): every poll,
  kept for a retention window (default 90 days -- see purge_raw.py). This is what
  ingest.py writes continuously, and what lets you recompute/redefine metrics later
  without re-collecting.
- PERMANENT LAYER (trains, train_station_stats, train_stats, station_stats,
  train_route_variants): compact running statistics -- counts, sums, sum-of-squares,
  and threshold-bucket counts, NOT a copy of every observation -- built by aggregate.py
  from completed trips in the raw layer, and kept forever. See README.md's "Two-layer
  architecture" section for the full reasoning.
- aggregation_state tracks which (trip_id, start_date) trips have already been folded
  into the permanent layer, so aggregate.py is idempotent and purge_raw.py knows what's
  safe to delete.
- PLATFORM LAYER (platform_journeys, platform_calls, platform_variants,
  platform_lead_time_stats, platform_aggregation_state): a parallel pipeline for the
  SIRI ET Lite feed (XML, separate protocol from GTFS-RT), which carries SNCF's actual
  confirmed platform assignments. Identity key is (train_number, calendar_date) instead
  of (trip_id, start_date) since SIRI has no trip_id. Unlike the GTFS-RT raw layer,
  platform_journeys/platform_calls are NOT append-only history -- they're UPSERTed to
  just the current/final state per stop, since that's all the product needs (the
  platform is only displayed ~15 min ahead of arrival anyway) and per-poll history here
  was the single biggest thing in the db for no real benefit. See the "PLATFORM LAYER"
  section below and siri_parse.py.

SQLite + WAL mode is enough for a single-writer collector run over days/weeks. Migrate
to Postgres/TimescaleDB once you're past feasibility and want concurrent readers or
continuous aggregates instead of a batch job.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
-- Caps how large the -wal file is allowed to stay after a checkpoint (2026-08-23,
-- after the -wal file was found to have grown to ~6.8GB and stayed there indefinitely:
-- a normal PASSIVE/auto checkpoint resets the WAL's internal pointer but does NOT
-- shrink the file on disk -- only a TRUNCATE-mode checkpoint does, and nothing was
-- ever triggering one. This makes SQLite auto-truncate back down to (approximately)
-- this limit after every checkpoint going forward, instead of the file silently
-- sitting at its historical peak size forever. 64MB is comfortably above the
-- ~3.9MB default auto-checkpoint threshold (wal_autocheckpoint=1000 pages) so it
-- doesn't fight normal operation, while still bounding worst-case waste from a
-- future write burst (e.g. another schema migration like 2026-08-18's). See
-- check_wal.py for the diagnostic that found this.
PRAGMA journal_size_limit=67108864;

-- One row per HTTP poll of a given feed (trip_updates or service_alerts).
-- raw_gzip holds the entire, untouched protobuf response: our insurance policy
-- against "we didn't think we'd need that field".
CREATE TABLE IF NOT EXISTS snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_name               TEXT NOT NULL,
    fetched_at_utc          TEXT NOT NULL,      -- when we issued the request (ISO8601 UTC)
    feed_header_timestamp   INTEGER,            -- feed.header.timestamp (epoch seconds, SNCF's clock)
    gtfs_realtime_version   TEXT,
    entity_count            INTEGER,
    http_status             INTEGER,
    fetch_duration_ms       INTEGER,
    content_length_bytes    INTEGER,
    raw_gzip                BLOB,               -- gzip(raw protobuf bytes), NULL if store_raw=False
    error                   TEXT                -- non-NULL if this poll failed
);
CREATE INDEX IF NOT EXISTS idx_snapshots_feed_time ON snapshots(feed_name, fetched_at_utc);

-- One row per trip_update entity, per snapshot. A given train's trip_id/start_date
-- will appear many times across snapshots as its delay evolves -- that repetition
-- IS the time series we need for reliability + recovery-pattern analysis.
CREATE TABLE IF NOT EXISTS trip_updates (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id                 INTEGER NOT NULL REFERENCES snapshots(id),
    entity_id                   TEXT,
    trip_id                     TEXT,
    route_id                    TEXT,
    direction_id                INTEGER,
    start_date                  TEXT,
    start_time                  TEXT,
    schedule_relationship        TEXT,
    commercial_train_number     TEXT,           -- parsed from trip_id, validated against trips.txt trip_headsign
    service_code                 TEXT,           -- raw operator/brand code from trip_id (e.g. "OUI"), always extracted
    train_type                  TEXT,           -- human label for service_code, only set when confidence is high -- see parse.py SERVICE_CODE_INFO
    origin_uic                  TEXT,           -- origin station UIC code, parsed from trip_id, validated against stop_times.txt
    destination_uic             TEXT,           -- destination station UIC code, parsed from trip_id, validated against stop_times.txt
    mission_code                 TEXT,           -- unlabeled 1-3 digit trip_id segment, meaning unconfirmed
    vehicle_id                  TEXT,
    vehicle_label                TEXT,
    trip_update_timestamp       INTEGER,
    trip_update_delay           INTEGER,
    stop_time_update_count      INTEGER
    -- NOTE: there is deliberately no per-row raw_json here. It used to duplicate, in
    -- uncompressed text, data already captured losslessly (and compressed ~5.5x) in
    -- snapshots.raw_gzip -- in testing it was the single largest thing in the database
    -- (~45% of total size) for zero additional information. Full entity JSON is still
    -- recoverable any time via extract_entity.py, which decompresses raw_gzip and
    -- re-parses it with the exact same code path (parse.entity_to_dict).
);
CREATE INDEX IF NOT EXISTS idx_tu_trip ON trip_updates(trip_id, start_date);
CREATE INDEX IF NOT EXISTS idx_tu_train_number ON trip_updates(commercial_train_number, start_date);
CREATE INDEX IF NOT EXISTS idx_tu_route ON trip_updates(origin_uic, destination_uic);
CREATE INDEX IF NOT EXISTS idx_tu_train_type ON trip_updates(train_type);
CREATE INDEX IF NOT EXISTS idx_tu_snapshot ON trip_updates(snapshot_id);

-- One row per stop_time_update within a trip_update.
CREATE TABLE IF NOT EXISTS stop_time_updates (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_update_id          INTEGER NOT NULL REFERENCES trip_updates(id),
    stop_sequence            INTEGER,
    stop_id                  TEXT,
    schedule_relationship    TEXT,
    arrival_delay            INTEGER,
    arrival_time              INTEGER,
    arrival_uncertainty      INTEGER,
    departure_delay          INTEGER,
    departure_time           INTEGER,
    departure_uncertainty    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stu_tripupdate ON stop_time_updates(trip_update_id);
CREATE INDEX IF NOT EXISTS idx_stu_stop ON stop_time_updates(stop_id);

-- One row per alert entity, per snapshot (service_alerts feed).
CREATE TABLE IF NOT EXISTS service_alerts (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id                INTEGER NOT NULL REFERENCES snapshots(id),
    entity_id                  TEXT,
    cause                      TEXT,
    effect                     TEXT,
    header_text                TEXT,
    description_text           TEXT,
    active_period_start        INTEGER,
    active_period_end          INTEGER,
    informed_entities_json     TEXT
    -- Same reasoning as trip_updates: no per-row raw_json. snapshots.raw_gzip is the
    -- full-fidelity copy; extract_entity.py rebuilds this row's JSON from it on demand.
);
CREATE INDEX IF NOT EXISTS idx_sa_snapshot ON service_alerts(snapshot_id);

-- UIC station code -> name cache, backed by SNCF's official "Gares de voyageurs"
-- reference dataset (ODbL, ressources.data.sncf.com). Populated lazily: ingest.py
-- looks up any origin_uic/destination_uic it hasn't seen before, once, and writes
-- the result here. Every later poll reads from this table instead of calling the
-- API again -- see stations.py.
CREATE TABLE IF NOT EXISTS stations (
    codes_uic           TEXT PRIMARY KEY,
    nom                 TEXT,
    libellecourt        TEXT,           -- station trigram, e.g. "MPL"
    segment_drg         TEXT,
    lon                 REAL,
    lat                 REAL,
    codeinsee           TEXT,
    sncf_id             TEXT,
    lookup_status       TEXT NOT NULL,  -- 'ok' | 'not_found'
    resolved_at_utc     TEXT NOT NULL,
    raw_json            TEXT
);

-- ============================================================================
-- PERMANENT LAYER -- built by aggregate.py from the raw layer, kept forever.
-- Running sums instead of stored observations: mean = sum/count, variance from
-- sum_sq, and threshold-bucket counts for "on time" / "5+ min late" / etc, matching
-- the MVP design in README.md rather than storing full histograms or percentiles.
-- Exact percentiles (P90/P95) need the raw layer directly, for as long as it's kept.
-- ============================================================================

-- Which (trip_id, start_date) trips have already been folded into the tables below.
-- Re-running aggregate.py only processes trips not yet in here -- safe to re-run,
-- and purge_raw.py refuses to delete raw data for a trip that isn't in here yet.
CREATE TABLE IF NOT EXISTS aggregation_state (
    trip_id             TEXT NOT NULL,
    start_date          TEXT NOT NULL,
    train_number        TEXT,
    cancelled            INTEGER NOT NULL DEFAULT 0,
    aggregated_at_utc   TEXT NOT NULL,
    PRIMARY KEY (trip_id, start_date)
);

-- Every distinct (origin_uic, destination_uic) pair observed for a given train_number,
-- with a running count. trains.most_common_origin/destination_uic is derived from
-- whichever variant has the highest count here -- NOT assumed fixed, because the same
-- train_number is confirmed (via trips.txt) to sometimes run to different destinations
-- on different days.
CREATE TABLE IF NOT EXISTS train_route_variants (
    train_number        TEXT NOT NULL,
    origin_uic           TEXT,
    destination_uic      TEXT,
    observed_count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (train_number, origin_uic, destination_uic)
);

-- "What train is this?" -- identity/summary info, refreshed each time aggregate.py
-- processes a trip for this train_number.
CREATE TABLE IF NOT EXISTS trains (
    train_number                   TEXT PRIMARY KEY,
    service_code                    TEXT,
    train_type                     TEXT,
    most_common_origin_uic          TEXT,
    most_common_destination_uic     TEXT,
    route_variant_count             INTEGER NOT NULL DEFAULT 0,
    trips_observed                  INTEGER NOT NULL DEFAULT 0,
    first_seen_date                 TEXT,
    last_seen_date                  TEXT,
    updated_at_utc                  TEXT NOT NULL
);

-- The core reliability table: per train, per station it stops at, per direction, per
-- day-type bucket (weekday/saturday/sunday -- see README for why not all 7 days, or
-- month/season, to avoid thousands of near-empty buckets). "Final" arrival/departure
-- delay per stop is the last-observed value before that stop dropped out of the feed
-- (the train passed it) -- see aggregate.py's resolve_final_delays().
CREATE TABLE IF NOT EXISTS train_station_stats (
    train_number             TEXT NOT NULL,
    station_uic               TEXT NOT NULL,
    direction_id              INTEGER NOT NULL DEFAULT -1,   -- -1 = unknown/not present in feed
    day_type                  TEXT NOT NULL,                 -- 'weekday' | 'saturday' | 'sunday'
    observations               INTEGER NOT NULL DEFAULT 0,    -- trips that reached this stop
    arrival_observations       INTEGER NOT NULL DEFAULT 0,
    sum_arrival_delay          INTEGER NOT NULL DEFAULT 0,    -- seconds
    sum_arrival_delay_sq       INTEGER NOT NULL DEFAULT 0,
    departure_observations     INTEGER NOT NULL DEFAULT 0,
    sum_departure_delay        INTEGER NOT NULL DEFAULT 0,
    sum_departure_delay_sq     INTEGER NOT NULL DEFAULT 0,
    on_time_count              INTEGER NOT NULL DEFAULT 0,    -- arrival delay < 5 min
    late_5_count               INTEGER NOT NULL DEFAULT 0,    -- arrival delay >= 5 min
    late_15_count              INTEGER NOT NULL DEFAULT 0,    -- arrival delay >= 15 min
    late_30_count              INTEGER NOT NULL DEFAULT 0,    -- arrival delay >= 30 min
    updated_at_utc             TEXT NOT NULL,
    PRIMARY KEY (train_number, station_uic, direction_id, day_type)
);
CREATE INDEX IF NOT EXISTS idx_tss_station ON train_station_stats(station_uic, day_type);

-- Per train, per day-type: end-to-end journey reliability (delay at the last observed
-- stop) plus cancellations. cancelled_count only counts trips the feed explicitly
-- marked CANCELED -- a train that never appeared in the feed at all isn't detectable
-- without cross-referencing the static schedule's calendar, which isn't wired up yet
-- (see README).
CREATE TABLE IF NOT EXISTS train_stats (
    train_number         TEXT NOT NULL,
    day_type              TEXT NOT NULL,
    observations           INTEGER NOT NULL DEFAULT 0,   -- trips that ran (not cancelled)
    cancelled_count        INTEGER NOT NULL DEFAULT 0,
    sum_final_delay         INTEGER NOT NULL DEFAULT 0,   -- seconds, at last observed stop
    sum_final_delay_sq      INTEGER NOT NULL DEFAULT 0,
    on_time_count           INTEGER NOT NULL DEFAULT 0,
    late_5_count            INTEGER NOT NULL DEFAULT 0,
    late_15_count           INTEGER NOT NULL DEFAULT 0,
    late_30_count           INTEGER NOT NULL DEFAULT 0,
    updated_at_utc          TEXT NOT NULL,
    PRIMARY KEY (train_number, day_type)
);

-- Per station, per train_type, per day-type: aggregate station reliability profile
-- ("18% of TGVs arrive >=15 min late at Lyon Part-Dieu"), independent of any one train.
CREATE TABLE IF NOT EXISTS station_stats (
    station_uic          TEXT NOT NULL,
    train_type            TEXT NOT NULL,   -- 'unknown' bucket for unmapped service_codes
    day_type               TEXT NOT NULL,
    observations            INTEGER NOT NULL DEFAULT 0,
    sum_arrival_delay        INTEGER NOT NULL DEFAULT 0,
    sum_arrival_delay_sq     INTEGER NOT NULL DEFAULT 0,
    on_time_count            INTEGER NOT NULL DEFAULT 0,
    late_5_count             INTEGER NOT NULL DEFAULT 0,
    late_15_count            INTEGER NOT NULL DEFAULT 0,
    late_30_count            INTEGER NOT NULL DEFAULT 0,
    updated_at_utc           TEXT NOT NULL,
    PRIMARY KEY (station_uic, train_type, day_type)
);

-- ============================================================================
-- PLATFORM LAYER -- SIRI ET Lite feed (XML, not protobuf -- see siri_parse.py and
-- ingest.py's poll_siri_feed()). Separate protocol, separate identity key: SIRI has
-- no trip_id/start_date, so trips are identified by (train_number, calendar_date)
-- instead, tracked in platform_aggregation_state. This is what carries SNCF's
-- actual confirmed platform (ArrivalPlatformName/DeparturePlatformName) -- data
-- GTFS-RT never has at all. RecordedCall = confirmed ("Confirmed"), EstimatedCall =
-- not yet confirmed ("Likely" once cross-referenced against platform_variants below).
--
-- UPSERT, not append-only, unlike the GTFS-RT raw layer above. Originally this was
-- one fresh row per journey/call per poll (same shape as trip_updates), but that's
-- what made platform_calls the single largest table in the db by a wide margin --
-- 13M+ rows from a day of 2-minute polling, almost all of it redundant restatements
-- of the same not-yet-final estimate. The product only ever needs the CURRENT/final
-- platform per stop (it's displayed at the station ~15 min ahead of arrival anyway,
-- see conversation 2026-08-18) -- there's no use for "what did we predict 40 minutes
-- ago and then again 38 minutes ago". So each poll now UPSERTs onto one row per
-- (train_number, calendar_date) / (..., stop_point_ref) instead of inserting a new
-- one, which keeps this table's size bounded by how many trains×stops actually run,
-- not by how many times they were polled. The one thing that DOES need a history
-- point, not just current state, is "how far ahead of arrival does SNCF actually
-- confirm the platform" (platform_lead_time_stats below) -- so the first moment a
-- platform goes non-NULL is captured once, in *_platform_first_confirmed_at_utc, and
-- never overwritten after that.
-- ============================================================================

-- One row per (train_number, calendar_date), continuously updated as newer polls
-- come in -- the SIRI analogue of trip_updates, but current-state instead of
-- one-row-per-poll (see note above).
CREATE TABLE IF NOT EXISTS platform_journeys (
    train_number                    TEXT NOT NULL,  -- TrainNumberRef -- joinable to trip_updates.commercial_train_number
    calendar_date                   TEXT NOT NULL,  -- derived from origin_aimed_departure_time, see siri_parse.calendar_date_from_iso -- best-effort, not a true SIRI field
    line_name                       TEXT,           -- PublishedLineName
    origin_name                     TEXT,
    destination_name                TEXT,
    product_category_ref            TEXT,           -- second, independent train-type signal -- not yet cross-validated against parse.py's service_code
    origin_aimed_departure_time     TEXT,
    destination_aimed_arrival_time  TEXT,
    call_count                      INTEGER,
    first_seen_snapshot_id          INTEGER REFERENCES snapshots(id),   -- which poll first reported this journey
    last_seen_snapshot_id           INTEGER REFERENCES snapshots(id),   -- most recent poll that still reported it
    last_updated_at_utc             TEXT NOT NULL,
    PRIMARY KEY (train_number, calendar_date)
);

-- One row per (train_number, calendar_date, stop_point_ref) -- the current/final call
-- info for that stop, continuously updated. call_type reflects the LATEST status seen
-- (Confirmed once a RecordedCall shows up, Likely/'estimated' until then); the
-- *_platform_first_confirmed_at_utc columns are the one piece of history kept, for
-- platform_lead_time_stats.
CREATE TABLE IF NOT EXISTS platform_calls (
    train_number                              TEXT NOT NULL,
    calendar_date                             TEXT NOT NULL,
    stop_point_ref                            TEXT NOT NULL,  -- SIRI's own stop identifier, e.g. "StopPoint:OCETrain-87481002" -- not yet mapped to stations.codes_uic
    call_type                                 TEXT NOT NULL,  -- 'recorded' | 'estimated' -- current status, can flip estimated->recorded across polls
    stop_point_name                           TEXT,
    aimed_arrival_time                        TEXT,
    expected_arrival_time                     TEXT,
    arrival_platform_name                     TEXT,           -- NULL until SNCF announces it
    arrival_platform_first_confirmed_at_utc   TEXT,           -- set once, first poll where arrival_platform_name went non-NULL; never overwritten after
    aimed_departure_time                      TEXT,
    expected_departure_time                   TEXT,
    departure_platform_name                   TEXT,
    departure_platform_first_confirmed_at_utc TEXT,           -- same idea, for departure
    last_updated_at_utc                       TEXT NOT NULL,
    PRIMARY KEY (train_number, calendar_date, stop_point_ref)
);
CREATE INDEX IF NOT EXISTS idx_pc_stop ON platform_calls(stop_point_ref);

-- Which (train_number, calendar_date) journeys have already been folded into the
-- permanent platform tables below -- the SIRI analogue of aggregation_state. Separate
-- table because the identity key is different (SIRI has no trip_id).
CREATE TABLE IF NOT EXISTS platform_aggregation_state (
    train_number        TEXT NOT NULL,
    calendar_date        TEXT NOT NULL,
    aggregated_at_utc    TEXT NOT NULL,
    PRIMARY KEY (train_number, calendar_date)
);

-- Every distinct platform observed as CONFIRMED (call_type='recorded' only -- estimated
-- platforms are still guesses and would pollute the "usual platform" signal) for a
-- given (train_number, stop_point_ref, call_field), with a running count. This IS the
-- "Likely" tier: when a live poll has no RecordedCall yet for a stop, the
-- highest-observed_count row here is the historical-prediction fallback -- Confirmed
-- (live) -> Likely (this table) -> Unknown (neither).
CREATE TABLE IF NOT EXISTS platform_variants (
    train_number         TEXT NOT NULL,
    stop_point_ref         TEXT NOT NULL,
    call_field             TEXT NOT NULL,    -- 'arrival' | 'departure'
    platform_name           TEXT NOT NULL,
    observed_count           INTEGER NOT NULL DEFAULT 0,
    last_observed_date       TEXT,
    PRIMARY KEY (train_number, stop_point_ref, call_field, platform_name)
);
CREATE INDEX IF NOT EXISTS idx_pv_lookup ON platform_variants(train_number, stop_point_ref, call_field);

-- How far ahead of the aimed time SNCF actually announces the platform, per
-- (train_number, stop_point_ref, call_field) -- running sums, same pattern as the
-- delay stats tables. lead_time_seconds = aimed_time - first_seen_confirmed_time.
-- The "first_seen_confirmed_time" half of that is captured at INGEST time now (see
-- platform_calls.*_platform_first_confirmed_at_utc above), not by scanning snapshot
-- history in aggregate.py -- aggregate.py just reads that column directly.
CREATE TABLE IF NOT EXISTS platform_lead_time_stats (
    train_number             TEXT NOT NULL,
    stop_point_ref             TEXT NOT NULL,
    call_field                 TEXT NOT NULL,    -- 'arrival' | 'departure'
    observations                 INTEGER NOT NULL DEFAULT 0,
    sum_lead_time_seconds        INTEGER NOT NULL DEFAULT 0,
    sum_lead_time_seconds_sq     INTEGER NOT NULL DEFAULT 0,
    never_confirmed_count        INTEGER NOT NULL DEFAULT 0,   -- journeys that ran with no platform ever announced
    updated_at_utc                TEXT NOT NULL,
    PRIMARY KEY (train_number, stop_point_ref, call_field)
);

-- Every poll attempt, success or failure -- lets you audit collector uptime
-- over a multi-day unattended run without grepping log files.
CREATE TABLE IF NOT EXISTS ingestion_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at_utc          TEXT NOT NULL,
    feed_name           TEXT NOT NULL,
    status              TEXT NOT NULL,      -- 'ok' | 'error'
    http_status         INTEGER,
    entity_count        INTEGER,
    duration_ms         INTEGER,
    error_message       TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_time ON ingestion_log(run_at_utc);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
