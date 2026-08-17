# TchouTchou data collector

Polls SNCF's national real-time feeds (via transport.data.gouv.fr) every 2 minutes,
logs raw + parsed data to a local SQLite database. Two-layer design: `ingest.py` writes
a raw layer continuously; `aggregate.py` folds completed trips into permanent reliability
statistics (per train, per station); `purge_raw.py` trims the raw layer once it's safely
aggregated. See "Two-layer architecture" below for the full reasoning.

## Setup

```
pip install -r requirements.txt
python peek.py
```

`peek.py` does one fetch and prints raw entities — useful for eyeballing real `trip_id`
samples. Train number, origin/destination, and 8 of 13 `service_code`s are now
validated against SNCF's static GTFS export and/or confirmed live (see "Known
unknowns" below for exactly what's confirmed vs. still a lower-confidence guess).

## Running the collection

```
# smoke test: one poll of both feeds, exits immediately
python ingest.py --once

# the actual 48-hour run
python ingest.py --duration-hours 48 --db tchoutchou.db

# indefinitely, until you Ctrl+C
python ingest.py --db tchoutchou.db
```

Runs in the foreground. For an unattended multi-day run, use `nohup`/`screen`/`tmux`
(Linux/Mac) or Task Scheduler / just leave the terminal open (Windows):

```
nohup python ingest.py --duration-hours 336 --db tchoutchou.db > /dev/null 2>&1 &
```

(336h = 2 weeks — see "How much data before the score means anything" below.)

Check progress anytime, while it's still running or after:

```
python stats.py --db tchoutchou.db
```

## What gets stored

Two feeds are polled: `trip_updates` (delay/position per train) and `service_alerts`
(disruptions, cancellations). Both are official SNCF real-time feeds, refreshed by
SNCF every 2 minutes — polling faster than that buys nothing.

For every poll:
- **`snapshots`**: one row, including the entire raw protobuf response gzip-compressed
  in `raw_gzip`. This is the safety net — if it turns out we need a field nobody
  thought to model, `extract_entity.py` can pull any entity's full JSON back out of it,
  any time, without needing to have stored that JSON per-row up front (see "Storage"
  below for why that distinction matters).
- **`trip_updates`** / **`stop_time_updates`**: one row per train / per stop-on-that-train,
  parsed out of the snapshot, including `commercial_train_number`, `service_code`,
  `train_type`, `origin_uic`, and `destination_uic` parsed from `trip_id` (see "Known
  unknowns" below for confidence level per field).
- **`service_alerts`**: one row per active disruption.
- **`stations`**: a cache, not a per-poll table. After each poll, `stations.py`
  collects every `origin_uic`/`destination_uic` not already in this table and resolves
  all of them in one batched request (`where=codes_uic in ("...","...",...)`, chunked
  at 50 codes per call) against SNCF's official "Gares de voyageurs" reference dataset
  (ODbL, `ressources.data.sncf.com`), storing name, trigram, and coordinates for each.
  Every later poll checks this table first, so a given station is looked up exactly
  once no matter how many trains or days reference it — and even the first poll, which
  might see 100+ distinct stations, only costs a couple of HTTP calls rather than one
  per station. France has a few thousand stations total, so the cache saturates within
  the first day or two. Disable with `--no-station-lookup` if you'd rather resolve UIC
  codes offline later against a static file instead.
- **`ingestion_log`**: one row per poll attempt, success or failure — for auditing
  collector uptime without grepping logs.

Nothing is filtered or downsampled. "Ingest as much as possible" was the brief, so
`snapshots.raw_gzip` (the full compressed feed, every poll) is the safety net for
anything not modeled in a structured column — see `extract_entity.py` to pull a given
entity's full JSON back out of it on demand.

### Storage, measured from a real 15-minute run

A 15-minute test run (9 poll cycles of both feeds) produced a 70.4 MB database. Broken
down with SQLite's `dbstat`, ~45% of that (31.7 MB) was a `raw_json` TEXT column that
used to be stored on every `trip_updates`/`service_alerts` row — a full uncompressed
JSON dump of that entity, duplicating data already captured, compressed, in
`snapshots.raw_gzip` (only 3.2 MB for the same 15 minutes, ~5.5x smaller per byte of
original data). That column has been removed; `extract_entity.py` reconstructs the
same JSON on demand from `raw_gzip` instead, so nothing is actually lost, it's just no
longer paid for on every single row.

Simulating the fix against that same real 15-minute run: **70.4 MB → 28.0 MB**, a 60%
cut. Scaled out from that measured rate (28.0 MB / 15 min):

| duration | est. size |
|---|---|
| 1 day | ~2.6 GB |
| 1 week | ~18 GB |
| 2 weeks | ~37 GB |
| 4 weeks | ~74 GB |

That's a real number, not a guess — check it against your own run early with `python
stats.py` (`database file size`) rather than assuming it holds, since actual train
volume varies by time of day. If even that's more than you want to commit to disk,
further options exist (not implemented, ask if you want them): dropping the
`idx_stu_stop` index during collection and building it only when you're ready to
analyze (~15% of remaining size), or trimming `stop_time_updates` to a shorter
look-ahead window instead of a train's full remaining journey (bigger savings, but
costs the "how far in advance was this delay visible" signal, which the reliability
scoring in the original pitch probably wants).

## Two-layer architecture: raw window + permanent aggregates

Storing every 2-minute observation forever doesn't scale and isn't what the product
needs — "train 9898 is on time 62% of the time, but averages +18 min by Lyon when it's
late at departure" is far more useful than one running average, but it doesn't require
keeping every individual observation around forever either. So:

- **Raw layer** (`snapshots`, `trip_updates`, `stop_time_updates`, `service_alerts`) —
  what `ingest.py` writes continuously, kept for a retention window (default 90 days).
  This is what lets you recompute or redefine a metric later without re-collecting —
  e.g. exact percentiles, or a new bucket threshold nobody thought of yet.
- **Permanent layer** (`trains`, `train_station_stats`, `train_stats`, `station_stats`,
  `train_route_variants`) — compact running statistics built from the raw layer by
  `aggregate.py`, kept forever. Not a copy of every observation: counts, sums, and
  sum-of-squares (mean/variance without storing each value), plus threshold-bucket
  counts (on-time / ≥5 / ≥15 / ≥30 min late), matching the MVP field list from the
  original design discussion.

```
GTFS-RT every ~2 min
        |
        v
  raw layer (90-day window)
        |
        v
  aggregate.py  (daily)
        |
   +----+----+--------+
   v         v         v
trains  train_station  station_stats
        _stats
        |
        v
  purge_raw.py  (after aggregation)
```

### Running it

```
python aggregate.py --db tchoutchou.db          # fold yesterday-and-older trips into permanent stats
python purge_raw.py --db tchoutchou.db --dry-run   # preview what's safe to delete
python purge_raw.py --db tchoutchou.db             # delete raw data already folded into stats, past retention
```

Run `aggregate.py` daily (the `schedule` skill, cron, or Task Scheduler all work) —
it's idempotent, tracked via `aggregation_state`, so re-running or missing a day is
harmless. Run `purge_raw.py` after it, on whatever cadence keeps disk usage where you
want it. **`purge_raw.py` refuses to delete a trip's raw data until `aggregate.py` has
folded it into the permanent tables** — verified in testing (a trip aggregated 3 days
ago gets purged at a 2-day retention setting; a trip that's old but never got
aggregated is skipped with a warning, not deleted).

### A wrinkle this surfaced: train numbers aren't a stable route key

Cross-referencing `trips.txt`, the same `train_number` showed up with **3 different
destination UICs** across different calendar dates (still the same `service_code`,
same leading trip_id segment). So a `trains` table can't just store one fixed
origin/destination per train — `most_common_origin_uic`/`most_common_destination_uic`
are the *most frequently observed* pairing (tracked via `train_route_variants`, one row
per distinct pair seen, with a count), not an assumption. `route_variant_count` tells
you how much a given train's route actually varies. `train_station_stats`, keyed by
individual station rather than a fixed route, isn't affected by this at all — which is
also why it's the table worth trusting most for the actual reliability product.

### "Final" delay per stop

For each stop a trip reports, `aggregate.py` uses the delay from the **last** snapshot
that still had that stop in its `stop_time_update` list — not the first prediction,
which can be hours stale (verified in testing: a stop predicted +15 min three hours out
that recovered to +2 min by the time the train reached it correctly aggregates as +2
min, on-time bucket, not +15 min).

### What this MVP intentionally leaves out

- **Exact percentiles (P90/P95).** These tables keep sums and sum-of-squares, which
  gives mean/variance for free, but not exact quantiles. Get those from the raw layer
  directly while it's still within the retention window.
- **Delay propagation** ("given +10 min at station A, what's the expected delay at
  station B") — one of the more interesting product features, but it's its own table
  and worth building once there's enough history in `train_station_stats` to validate
  against, rather than guessing the schema now.
- **True cancellation detection.** `train_stats.cancelled_count` only counts trips the
  feed explicitly marked `CANCELED`. A train that never appears in the real-time feed
  at all looks identical to "wasn't scheduled today" without cross-referencing the
  static schedule's `calendar_dates.txt` — not wired up yet.
- **Day-of-week granularity beyond weekday/Saturday/Sunday**, or month/season slicing —
  deliberately coarse to avoid splitting a train's history into dozens of near-empty
  buckets this early. The raw window is exactly what lets you recompute a finer slice
  later if the data supports it.

## Platform data (SIRI ET Lite)

GTFS-RT `trip_updates` never carries a platform/track number. SNCF's separate **SIRI ET
(Estimated Timetable) Lite** feed does — an XML protocol (not protobuf), fetched from
`https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-estimated-timetable` by
`ingest.py`'s `poll_siri_feed()`, parsed by `siri_parse.py`.

Each `EstimatedVehicleJourney` reports a list of calls, each either a `RecordedCall`
(SNCF has confirmed what happened at that stop — this is where a real platform shows
up, e.g. `DeparturePlatformName`) or an `EstimatedCall` (still a prediction, not yet
confirmed). That distinction is captured directly as `call_type` on each row, not
inferred — it's the natural confidence hierarchy the product wants:

**Confirmed** (a live `RecordedCall` has a platform right now) → **Likely** (no live
confirmation yet, but `platform_variants` shows this train has used one platform
consistently at this stop historically) → **Unknown** (neither).

This mirrors the two-layer design used for delays: a raw layer that captures every
poll (`platform_journeys`, one row per journey per snapshot; `platform_calls`, one row
per call per journey), and a permanent layer built by `aggregate.py`'s
`process_platform_trip()`:

- **`platform_variants`** — every platform ever seen as **confirmed** (`recorded`
  only — an estimated platform, if SIRI ever populates one, is still a guess and would
  pollute this signal) for a `(train_number, stop_point_ref, call_field)`, with a
  running count. The `platform_variants` row with the highest count for a given
  train+stop+field is the "Likely" answer when there's no live confirmation.
- **`platform_lead_time_stats`** — how far ahead of the aimed time SNCF actually
  confirms the platform, as running sums (`sum_lead_time_seconds` /
  `sum_lead_time_seconds_sq`) plus a `never_confirmed_count` for journeys that ran with
  no platform ever announced. Useful for setting expectations in the product ("platform
  is usually confirmed ~12 minutes before departure at this station").

SIRI has no `trip_id`/`start_date` the way GTFS-RT does, so the identity key here is
`(train_number, calendar_date)` instead — tracked in `platform_aggregation_state`,
`calendar_date` being a best-effort derivation from `OriginAimedDepartureTime`
(`siri_parse.calendar_date_from_iso()`), not a true SIRI field. `purge_raw.py` purges
`platform_journeys`/`platform_calls` the same "only if already aggregated" way it
purges the GTFS-RT raw layer, gated on `platform_aggregation_state`.

**Confirmed against a live poll (2026-08-17):** a single `--once --feeds siri_et` run
returned 3,024 journeys / 33,331 calls. Two things the synthetic test couldn't confirm:

- **`stop_point_ref` format is `FR:ScheduledStopPoint::87734319`**, not the
  `StopPoint:OCE87481002`-style ref the one confirmed example (train 859422) used —
  SNCF appears to use more than one ref scheme across services. Either way, the UIC
  code is the trailing 5-8 digits: checked against all 3,589 distinct refs from this
  poll, `aggregate.py`'s existing `extract_uic()` (built for GTFS-RT `stop_id`) matched
  100% of them. So platform data IS joinable to `stations.codes_uic` -- via that same
  helper, no new parsing needed -- but the join itself isn't wired into any table yet.
- **`product_category_ref` is a real, richer enum** than the single "TER" example
  suggested: observed values include `FR:TypeOfProductCategory::highSpeedRail::`,
  `regionalRail`, `suburbanRailway`, `tramTrain`, `longDistance`, `interregionalRail`,
  `crossCountryRail`, `local`, `localBus`, `regionalCoach`, `railReplacementCoach`,
  `railShuttle`. Still not cross-validated against `parse.py`'s `service_code`-derived
  `train_type` -- worth doing once there's aggregated history to compare against.
- **Confirmation rate looks meaningful, not sparse:** of 10,460 `recorded` calls in
  this one poll, 7,849 (75%) carried an actual platform name -- validates that the
  Confirmed/Likely split is worth the table, not just a theoretical distinction.

**Still not done:** the UIC-derived `station_uic` isn't stored as a column on
`platform_variants`/`platform_lead_time_stats` -- deliberately, to avoid an ALTER TABLE
mid-collection now that real data is flowing. Add it (and backfill) once there's a
concrete need to query platform stats by station rather than by `stop_point_ref`.

## Known unknowns (and what's actually validated now)

`trip_id` looks like this:

```
OCESN9898F1187_F:OUI:FR:Line::4FA25873-A63A-4A2D-B62F-EF950E45D8A9::87773002:87191007:12:1349:20260821
[OCESN][train#][F/R][...]:[brand]:[country]:Line::[route uuid]::[origin UIC]:[dest UIC]:[mission?]:[run id]:[date]
```

- **Train number is now properly validated, not guessed.** `parse_trip_id()` extracts
  the digits between the operator prefix (`OCESN`) and the following single uppercase
  letter (`F` or `R`). This was checked against the *entire* static GTFS export in
  `Export_OpenData_SNCF_GTFS_NewTripId/trips.txt` — 46,192 trips, comparing the
  extracted number against SNCF's own `trip_headsign` field for that same trip. **Zero
  mismatches**, across all 4 operator prefixes present (`OCESN`, `OCESA`, `OCEEA`,
  `OCELO`) and both suffix letters seen (`F`, `R`). Re-run this check yourself anytime
  with `python validate_extraction.py`.

  **This corrects an earlier wrong answer.** An earlier version of this heuristic took
  "the segment right before the trailing 8-digit date" as the train number — for the
  sample above that returned `1349`, which turned out to be some other run/schedule
  identifier, not the train number. The real train number is **9898**. That bug wasn't
  caught by eyeballing one live sample (both numbers *look* plausible) — it only showed
  up by cross-referencing against SNCF's own static ground truth (`trip_headsign` in
  `trips.txt`) across thousands of trips. Worth remembering: a heuristic that "looks
  right" on one example isn't validated until it's checked against many.

- **Origin/destination UIC extraction was validated the same way** — comparing the
  parsed `origin_uic`/`destination_uic` against each trip's actual first/last stop (by
  `stop_sequence`) in `stop_times.txt`, on a random sample of ~500 trips: also zero
  mismatches. This part of the parse was right from the start; only the train number
  needed correcting.

- **Train type / brand (`service_code`, `train_type`) — discovered and evidence-checked,
  not assumed from one example.** The segment right after the train number (`OUI` in the
  sample above) is a short operator/brand code. `service_code` is a direct, always-present
  extraction (validated: all 46,192 trips have one, zero `None`s). Mapping that code to a
  human label is a separate step, done by cross-referencing every trip carrying each code
  against `routes.txt` (route names/types) and destination UIC country prefixes — see
  `parse.py`'s `SERVICE_CODE_INFO` for the evidence behind each one, or re-run
  `python validate_extraction.py` to reproduce the breakdown. Result, by volume:

  | code | trips | train_type | confidence |
  |---|---|---|---|
  | TER | 30,366 | TER | high — exact name match |
  | OUI | 7,498 | TGV INOUI | high — matches the confirmed train 9898 example |
  | CTE | 5,363 | Car TER (TER replacement bus) | high — **confirmed live**: a real trip's stop_id spelled out "Car TER" directly, same method as CRE below |
  | IC | 748 | Intercités | high — standard abbreviation, known IC lines |
  | OGO | 578 | OUIGO | high — distinct route_id scheme, matches OUIGO numbering |
  | TT | 389 | Tram-Train | high — all routes are route_type=0 (tram) |
  | CRE | 384 | Car à réservation (reserved coach / rail-replacement bus) | high — **confirmed live**: a real train's next stop_id literally read `StopPoint:OCECar à réservation-87698902`; also explains why that UIC wasn't in the station reference dataset (it's a coach stop, not a rail station) |
  | LYR | 254 | TGV Lyria | high — destinations include Swiss (85xxxxxx) UIC codes |
  | ICE | 237 | ICE (France-Germany) | high — destinations include German (80xxxxxx) UIC codes |
  | ICN | 215 | *(unmapped)* | low — train numbers loosely resemble night-train ranges |
  | TRN | 90 | *(unmapped)* | low — tiny sample, unusual 2-digit numbering |
  | NAV | 68 | Navette | high — matches "shuttle", distinctive numbering |
  | NA | 2 | *(unmapped)* | low — 2 trips total, not enough to interpret |

  `train_type` in the database is only populated for the "high" confidence rows — the
  "unmapped" ones stay `NULL` rather than asserting a guess, but `service_code` is always
  there raw so you can revisit them later (or look one up on SNCF Connect directly if you
  need to know sooner). `discover_service_codes()` in `validate_extraction.py` also flags
  any *new* code it finds that isn't in `SERVICE_CODE_INFO` yet, in case SNCF introduces one.

- **The 2-digit `mission_code` segment** (`12` in the example) is still unmodeled —
  captured raw, meaning unconfirmed.

- **Both extractions fall back to lower-confidence heuristics** for any trip_id shape
  that doesn't match the validated pattern (didn't happen anywhere in the current
  static export, but a live feed could in principle differ, e.g. after a schedule
  change) — so nothing crashes, it just flags lower confidence for that row.

- **Re-validate whenever you refresh the static GTFS export**, since this is a
  discovered convention, not a documented spec: `python validate_extraction.py
  --gtfs-dir <path to a newer export>`. It's a dev-time check only — production
  ingestion (`ingest.py`) never touches `trips.txt`/`stop_times.txt`, only the compiled
  regex, exactly as intended.

- **Station name resolution confirmed working.** Querying `ressources.data.sncf.com`
  for UIC `87773002` returns `Montpellier Saint-Roch` — matches the trip_id sample
  above (a TGV INOUI departing Montpellier at 06:29 is entirely plausible). Automated
  via `stations.py`, batched and cached; see "What gets stored" above.

- **Note on this sample:** its `trip_id` trailing date (`20260821`) didn't match its
  `start_date` field (`20260817`) — a 4-day gap. Not yet understood (could be a
  look-ahead schedule reference, a static-data effective date, or something feed-specific).
  Doesn't affect the train number parsing, but worth a second look once more data is in.
- **Don't run the DB file from a cloud-synced folder** (Dropbox/OneDrive/etc). SQLite's
  WAL mode needs real filesystem locking; on at least one network-mounted filesystem
  tested here it threw `disk I/O error`. Run it from a plain local disk path.

## Legal: what you can and can't do with this data

Source: SNCF's [dataset page](https://transport.data.gouv.fr/datasets/horaires-sncf) and
transport.data.gouv.fr's [ODbL + Conditions Particulières
page](https://doc.transport.data.gouv.fr/le-point-d-acces-national/cadre-juridique/conditions-dutilisation-des-donnees/licence-odbl).

- **License:** ODbL 1.0, plus "Conditions Particulières d'Utilisation" that
  transport.data.gouv.fr negotiated with the ecosystem to narrow the share-alike clause
  (article 4.4), which is written broadly enough to otherwise be unworkable.
- **Commercial use is explicitly allowed**, including subscriptions and paid tiers.
- **Share-alike only applies to "derived databases" of the *same nature, granularity,
  temporal conditions, and geographic scope* as the source.** Their own worked example
  table draws the line directly around your use case:
  - "Real-time next-arrival times at bus stops" → **not** required to reshare (different
    temporal conditions from the static schedule feed).
  - "Prediction of bike availability in 10 minutes" → **not** required to reshare (same
    logic: it's a prediction, not a repackaging).
  - By the same reasoning, a computed Health Score, delay-probability model, or
    connection-risk score is a "création produite" (produced work) — not a derived
    database — because it has different temporal/statistical nature than the raw
    trip_update feed. **Not** subject to share-alike.
  - Correcting or adding raw data of the *same* kind (e.g. fixing a stop's coordinates)
    **is** subject to share-alike — that's the case you're not in.
- **Attribution is still mandatory regardless of produced-work status**: credit SNCF as
  a data source and reference the ODbL license, e.g. in an "about"/"legal" screen in the
  app (the license text explicitly allows for this when a dedicated menu/page is needed
  due to a small screen).
- **Real-time-specific note found on SNCF's own API docs (for their direct API, not this
  proxy feed):** caching of real-time data is only permitted for 60 seconds before
  re-querying. Not confirmed whether this specific clause applies to the
  transport.data.gouv.fr proxy feed used here (it publishes its own 2-minute refresh
  cadence, which we're matching) — worth a direct confirmation with SNCF's open data
  team (`Data_Office_Secretariat_General_SA_Voyageurs@sncf.fr`, listed on the dataset
  page) before scaling this past feasibility, particularly for the B2B API tier.
- This is a summary for planning purposes, not legal advice — before monetizing (the
  Premium tier and especially the B2B analytics API), get an actual read from someone
  qualified, particularly on where "produced work" stops covering you once you're
  reselling computed scores at scale.

## Next steps after this

1. **Smoke-test the SIRI integration against the live feed first.** Everything platform-
   related (`siri_parse.py`, `poll_siri_feed()`, `process_platform_trip()`) has only been
   verified against synthetic XML built to match confirmed real examples — never an
   actual live poll. Run `python ingest.py --once --feeds siri_et` and check: does
   `platform_journeys`/`platform_calls` populate as expected, and — the one open
   question from "Platform data" above — does `stop_point_ref` (e.g.
   `StopPoint:OCE87481002`) actually embed the same UIC code (`87481002`) used elsewhere,
   so it can eventually be joined against `stations.codes_uic`?
2. Kick off the multi-day run (recommend 2+ weeks, not 48h — see below), now with all
   three feeds (`trip_updates`, `service_alerts`, `siri_et`).
3. Once you have a day or two of history, start running `aggregate.py` daily (or set it
   up as a scheduled task) so `trains`/`train_station_stats`/`train_stats`/`station_stats`
   *and* `platform_variants`/`platform_lead_time_stats` start filling in — that's what
   you'd actually query for reliability/platform numbers, not the raw layer directly.
4. Run `purge_raw.py` on whatever cadence keeps disk usage in check, once there's enough
   aggregated history that you're comfortable trimming the raw window (now also purges
   `platform_journeys`/`platform_calls`, same "only if aggregated" rule).
5. Once `train_station_stats` has real history, delay-propagation ("given +10 min at
   station A, what's expected at station B") is the natural next table to build.
6. Once `platform_journeys` has real history, cross-validate `product_category_ref`
   against `parse.py`'s `service_code`-derived `train_type` — a second independent
   signal for train type, currently unused.

### Why 48 hours probably isn't enough

A punctuality percentage computed from ~1 day of runs for a given train isn't a
reliability score — 88% vs 58% on-time only means something once it's averaged over
enough days to smooth out one-off disruptions (strikes, engineering works, weather).
Treat the first 48h as a pipeline correctness check (does ingestion survive
unattended, are trip IDs stable, does the train-number heuristic hold up), and plan
for 2-4 weeks of continuous collection before computing anything you'd show a user.
