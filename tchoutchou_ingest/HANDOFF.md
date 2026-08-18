# TchouTchou — Data Pipeline Handoff

Status as of 2026-08-19. This covers the SNCF data collection pipeline: what exists,
what's been validated against real data, what's still open, and how to pick it up on
the VPS.

**Current state, in one paragraph**: the collector has been running on the VPS since
2026-08-17, survived a platform-layer schema migration on 2026-08-18 (see "Migrating an
existing VPS db" below), and is now on a 5-day raw-retention window due to VPS disk
constraints (~23GB free as of 2026-08-18). Git is set up and has been pushed to
multiple times (see "Git" below — no init needed, that step's done). Actively in
progress: validating the new SIRI upsert logic against real traffic before deciding
whether to drop its raw_gzip safety net (see "SIRI upsert validation" below).

## What TchouTchou is

A French train intelligence platform: predictive reliability, live tracking, crowd
insights, and deep-linking to SNCF Connect for booking. This pipeline is the data
foundation underneath that — it doesn't build any product features yet, it collects
and aggregates the raw material those features will need.

## What's built

Two SNCF open-data feeds, each with its own raw + permanent-aggregate pipeline:

1. **GTFS-RT** (`trip_updates` + `service_alerts`) — protobuf feeds giving live delay
   predictions per stop, polled every 2 minutes via the transport.data.gouv.fr proxy.
2. **SIRI ET Lite** (`siri_et`) — a separate XML feed giving SNCF's actual *confirmed*
   platform assignments, which GTFS-RT never carries at all.

Both write into the same layered SQLite schema:

- **Raw layer** (`snapshots`, `trip_updates`, `stop_time_updates`, `service_alerts`) —
  every GTFS-RT poll, kept for a retention window (5 days as of 2026-08-18, down from an
  original 90-day default — see the `--retention-days 5` note further down; short for now
  due to VPS disk constraints), then purged by `purge_raw.py` once folded into the
  permanent layer. Retained on a window (rather than
  deleted immediately after aggregation) for two reasons: (1) it lets you recompute or
  redefine the permanent stats later without re-collecting — useful while the
  aggregation logic is still evolving (see the `stop_sequence` bug fixed 2026-08-17,
  which needed raw data to diagnose and re-run against); (2) it's the only place exact
  percentiles (P90/P95) live — the permanent layer only keeps sums/sum-of-squares
  (mean/variance), not full histograms.
- **Platform layer** (`platform_journeys`, `platform_calls`) — SIRI's confirmed/likely
  platform data. **Redesigned 2026-08-18** from raw per-poll history to an UPSERT-based
  design: each row holds only the current/final value per (train_number, calendar_date)
  or per stop, never a growing history of every poll. This was a direct, deliberate
  product-driven simplification: the platform feature ("70% likely on platform E") and
  SNCF's own real-world platform display (~15 min before arrival) only ever need the
  settled final value, not the intermediate estimates leading up to it. Consequence:
  there's no per-poll history to retain or purge here at all — a row already IS the
  compact answer, bounded by trains × days rather than trains × days × polls. See
  `db.py`'s schema comments and `ingest.py`'s `poll_siri_feed()` for the mechanics.
- **Permanent layer** (`trains`, `train_station_stats`, `train_stats`, `station_stats`,
  `train_route_variants`, `platform_variants`, `platform_lead_time_stats`) — compact
  running statistics (counts, sums, sum-of-squares, threshold buckets) built from
  completed trips by `aggregate.py`, kept forever.

Supporting pieces: a UIC-code→station-name cache (`stations.py`, batched + cached
lookups against SNCF's reference API), `purge_raw.py` (deletes raw data once it's been
folded into the permanent layer, never before), and `extract_entity.py` (reconstructs
full entity JSON from the compressed raw blob on demand, since no per-row raw JSON is
stored — that was cut after it turned out to be ~45% of database size for zero extra
information).

Full architecture, schema, and reasoning is in `README.md` — this doc is the shorter
"what do I do next" version.

## File inventory

| File | Purpose |
|---|---|
| `ingest.py` | Main collector. Polls all three feeds, writes raw layer. Run this continuously. |
| `parse.py` | GTFS-RT protobuf → row parsing; trip_id decoding (train number, service code, train type, origin/destination UIC). |
| `siri_parse.py` | SIRI XML → row parsing for the platform feed. |
| `db.py` | Schema (`SCHEMA` string) + `connect()`. |
| `stations.py` | UIC code → station name cache/lookup. |
| `aggregate.py` | Folds completed trips from the raw layer into permanent stats. Run daily. |
| `purge_raw.py` | Deletes raw data already aggregated, past the retention window. Run daily, after aggregate.py. |
| `extract_entity.py` | Debug tool — rebuilds full entity JSON from a raw snapshot. |
| `dashboard.html` | Client-side (sql.js, no server) viewer for a `.db` file — Overview + Train Lookup tabs. Open directly in a browser. |
| `monitor_snapshot.py` | Exports a small (~MB) health-check snapshot of a multi-GB db — row counts, full permanent + platform layers, a sample of the raw layer. Use this to check on the VPS db without transferring the whole file. |
| `check_raw_blob_share.py` | One-off diagnostic: how many bytes of the db are `snapshots.raw_gzip` blobs vs everything else, broken down by feed. Informs the `--no-raw` decision. |
| `compare_platform_snapshots.py` | Diffs two `monitor_snapshot.py` exports to catch silent platform-data regressions (a confirmed platform going blank, or recorded→estimated) in the new SIRI upsert logic, plus a spot-check list for manual verification against reality. See "SIRI upsert validation" below. |
| `cross_validate_train_type.py` | Joins `trip_updates`/`platform_journeys` on train_number+date to cross-check `service_code`-derived `train_type` against SIRI's `line_name`/`product_category_ref` — decodes currently-unmapped codes (`ICN`/`TRN`/`NA`) from real traffic and flags already-mapped codes with inconsistent line_name patterns. |
| `find_examples.py`, `stats.py`, `peek.py`, `validate_extraction.py` | Ad hoc exploration/validation scripts used during development, not part of the running pipeline. |
| `getplatform.py`, `testsncf.py` | Original exploration scripts (yours) that the pipeline grew out of. Kept for reference. |
| `requirements.txt` | `requests`, `gtfs-realtime-bindings` — the only two non-stdlib dependencies. |
| `README.md` | Full architecture writeup, storage measurements, legal summary, known unknowns. |

## Validated against real data (not just assumptions)

- **Train number extraction** (`parse.py`'s trip_id regex) — checked against all 46,192
  trips in the static `trips.txt`, zero mismatches.
- **Service code → train type mapping** — TER, TGV INOUI, OUIGO, Intercités, TGV Lyria,
  France-Germany ICE, Navette, Car TER, Car à réservation all confirmed "high
  confidence" via live examples. `ICN`, `TRN`, and `NA` are still unmapped/low
  confidence.
- **Station name resolution** — confirmed correct against a real UIC lookup
  (Montpellier Saint-Roch).
- **Storage rate** — original estimate (~2.6 GB/day, from a 2-hour sample) turned out to
  be ~2.3x too low: a real 23.14h VPS run measured ~247 MB/hour (~5.9 GB/day), traced to
  under-accounting for indexed row overhead in the old append-only `platform_calls`
  table specifically (13.27M rows in that run alone). The 2026-08-18 platform-layer
  redesign (UPSERT to final state, see above) removes that growth source entirely going
  forward — `platform_journeys`/`platform_calls` are now bounded by trains × days, not
  trains × days × polls. Permanent layer still projected at a ~100-150 MB ceiling
  long-term (19,080 train numbers × ~163,529 train×station pairs).
- **SIRI platform feed, live** (2026-08-17): one poll returned 3,024 journeys / 33,331
  calls. Confirmed two things the earlier synthetic test couldn't:
  - `stop_point_ref` format is `FR:ScheduledStopPoint::87734319` (not the
    `StopPoint:OCE...` format the one hand-checked example used) — but the trailing
    UIC digits are always there: checked against all 3,589 distinct refs in that poll,
    100% matched.
  - `product_category_ref` is a real, richer enum (`highSpeedRail`, `regionalRail`,
    `suburbanRailway`, `tramTrain`, `longDistance`, etc. — 12 values) than the single
    "TER" example suggested.
  - 75% of confirmed (`RecordedCall`) platform observations actually carried a platform
    name — the Confirmed/Likely split is a real signal, not a theoretical one.
- **`raw_gzip` blob share** (2026-08-19, `check_raw_blob_share.py`): 32.6% of db size
  (777MB of 2.33GB at the time). Not evenly split across feeds — SIRI's XML blob
  averages 710 KB/poll vs 118-171 KB/poll for the two GTFS-RT feeds, so SIRI alone is
  ~71% of all blob bytes despite equal poll counts. Informs the `--no-raw` decision: see
  "SIRI upsert validation" below for why SIRI's blob is being kept a bit longer than
  GTFS-RT's despite costing more.
- **SIRI upsert validation** (ongoing, started 2026-08-19): the 2026-08-18 platform-layer
  redesign means `platform_calls` no longer keeps poll history — each row is now the
  *only* record of that stop, so a silent upsert bug (e.g. a stale poll blanking out an
  already-confirmed platform) would be undetectable without the raw blob. Two checks in
  progress before deciding to drop SIRI's raw blob:
  1. `compare_platform_snapshots.py` diffs two `monitor_snapshot.py` exports taken hours
     apart and flags any confirmed platform that went blank, or any call that reverted
     recorded→estimated, without a legitimate reassignment. Verified against synthetic
     fixtures to correctly catch an injected regression and correctly ignore a
     legitimately-still-confirmed call.
  2. Manual spot-checks against SNCF Connect/real departure boards. First round
     (2026-08-19, overnight/thin traffic): 2/2 correct — train UMOL09 (RER A) and
     164405 (Transilien N) both matched reality. Re-run planned during daytime service
     for a larger sample.
  Once this has run clean for a while, drop SIRI's raw blob (bigger win than GTFS-RT's,
  see blob-share numbers above); keep GTFS-RT's regardless since it still has full poll
  history in `trip_updates`/`stop_time_updates` and the blob there is a cheaper,
  lower-stakes safety net.

## What's NOT done yet

- **`station_uic` isn't stored as a column** on `platform_variants` /
  `platform_lead_time_stats`, even though it's derivable (deliberately, to avoid an
  ALTER TABLE mid-collection). Add it once you actually need to query platform stats by
  station rather than by raw `stop_point_ref`.
- **`product_category_ref` not cross-validated** against `parse.py`'s
  `service_code`-derived `train_type` — a second independent train-type signal,
  currently unused.
- **True cancellation detection** — `cancelled_count` only counts trips GTFS-RT
  explicitly marked `CANCELED`. A train that never appears in the feed at all looks
  identical to "wasn't scheduled today" without cross-referencing the static
  `calendar_dates.txt`, which isn't wired up.
- **Delay propagation** ("+10 min at station A → expected delay at station B") — natural
  next table once `train_station_stats` has real history to validate against.
- **Exact percentiles (P90/P95)** — the permanent layer keeps sums/sum-of-squares
  (mean/variance), not full histograms. Get exact quantiles from the raw layer while
  it's within the retention window.
- **Raw gzip blobs (`snapshots.raw_gzip`) not dropped yet** — deferred on purpose
  (2026-08-18). `--no-raw` already exists and is the single biggest remaining lever on
  db size, but turning it on by default loses the ability to re-diagnose a parsing bug
  after the fact without re-collecting (this is exactly how the `stop_sequence` bug was
  confirmed and fixed on 2026-08-17). Turn it on once parsing has been running clean for
  a while and that safety net isn't earning its disk cost anymore.

## Git

Repo: `https://github.com/leachtimothy2403-sketch/TchouTchou.git` — **set up and in use**,
pushed to multiple times since. `.gitignore` at `C:\Users\leach\tchoutchou\.gitignore`
excludes `*.db`, `*.log`, `__pycache__/`, and the large SNCF static-schedule export.

Workflow this project has been using: edits happen locally in
`C:\Users\leach\tchoutchou`, then get committed and pushed **from your Windows
machine** — there's no way to push from a sandboxed session (no stored GitHub
credentials, and the mounted-folder bridge can't reliably do git's own locking either).
After pushing, `git pull` on the VPS to pick it up, and restart the `TchouTchouIngest`
service only if `ingest.py`/`db.py`/`parse.py`/`siri_parse.py` actually changed —
standalone scripts (`monitor_snapshot.py`, `check_raw_blob_share.py`,
`compare_platform_snapshots.py`, doc updates) don't need a restart, the service doesn't
touch them.

```powershell
cd C:\Users\leach\tchoutchou
git add <changed files>              # exclude getplatform.py/testsncf.py unless you
git commit -m "..."                  # actually edited them -- their diffs vs the repo
git push                             # are just line-ending noise, not real changes
```

## VPS deployment (Windows)

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
# reopen PowerShell so PATH updates

git clone https://github.com/leachtimothy2403-sketch/TchouTchou.git C:\TchouTchou
cd C:\TchouTchou\tchoutchou_ingest
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Test first, for 2 hours, in the foreground** (not as a service yet):
```powershell
python ingest.py --duration-hours 2 --db tchoutchou.db --log-file tchoutchou_ingest.log
```
Check afterward: entity counts look sane across all three feeds each cycle, no
repeated errors in the log, and GTFS-RT raw-layer growth is in line with the corrected
measured rate, ~247 MB/hour (see storage rate note above) — the platform layer
(`platform_journeys`/`platform_calls`) should now stay near-flat regardless of run
length, since it's upserted to final state rather than growing per poll.
`aggregate.py` won't have anything to process yet — trips need to be at least a day old.

## Migrating an existing VPS db to the new platform-layer schema (2026-08-18)

If you already have a running collector (e.g. the `tchoutchou.db` that grew to 5.7GB),
its `platform_journeys`/`platform_calls` tables are the OLD append-only schema and are
**incompatible** with the new `ingest.py`/`aggregate.py`/`dashboard.html` — column names
changed (`pj.id`/`pj.snapshot_id` and `platform_journey_id` no longer exist) and the
insert logic is now UPSERT-based, which requires the new composite `PRIMARY KEY`. There
is no in-place migration path (the old rows are raw poll history that the new schema has
no slot for), and per the retention rationale above, that old raw history isn't worth
keeping — its only value was as input to `aggregate.py`, and it's either already been
aggregated (permanent stats already reflect it) or it's an acceptable loss.

If `aggregate.py`/`purge_raw.py` have never been run yet (i.e. the Task Scheduler jobs
below were never set up), do that here too, in the same stop/start window — they touch
only the GTFS-RT tables, not `platform_journeys`/`platform_calls`, so they don't
conflict with the drop/recreate below. Doing it all in one pass also means a single
`VACUUM` at the end reclaims space from both the purge and the dropped platform tables,
instead of vacuuming twice (VACUUM is slow and briefly needs ~2x disk space, so avoid
running it more than necessary).

```powershell
& $nssm stop TchouTchouIngest
cd C:\TchouTchou\tchoutchou_ingest
git pull
.\.venv\Scripts\Activate.ps1

# only needed the first time -- skip these two lines if the Task Scheduler jobs below
# are already set up and have been running
python aggregate.py --db tchoutchou.db
python purge_raw.py --db tchoutchou.db --retention-days 5

# platform-layer schema migration
python -c "
import sqlite3
conn = sqlite3.connect('tchoutchou.db')
conn.execute('DROP TABLE IF EXISTS platform_journeys')
conn.execute('DROP TABLE IF EXISTS platform_calls')
conn.commit()
conn.close()
"
python -c "import db; c = db.connect('tchoutchou.db'); c.close()"   # recreates both tables with the new schema

# one VACUUM reclaims space from BOTH the purge above and the dropped platform tables --
# this is what brings the 5.7GB file back down
python -c "import sqlite3; sqlite3.connect('tchoutchou.db').execute('VACUUM')"

& $nssm start TchouTchouIngest
```

Then set up (or confirm) the Task Scheduler jobs below so `aggregate.py`/`purge_raw.py`
keep running automatically going forward -- don't rely on manual runs.

Finally, confirm it's healthy: check `tchoutchou_ingest.log` for a few clean poll cycles,
and re-run `monitor_snapshot.py` (also updated for the new schema — copies
`platform_journeys`/`platform_calls` in full now instead of sampling them, since they're
small and bounded) to sanity-check row counts and sizes remotely.

**Once that's clean, run it for real as a Windows service** (no `--duration-hours`, so
it runs indefinitely and NSSM restarts it if it crashes):
```powershell
Invoke-WebRequest -Uri "https://nssm.cc/ci/nssm-2.24-101-g897c7ad.zip" -OutFile "$env:TEMP\nssm.zip"
Expand-Archive -Path "$env:TEMP\nssm.zip" -DestinationPath "C:\nssm"
$nssm = "C:\nssm\nssm-2.24-101-g897c7ad\win64\nssm.exe"

& $nssm install TchouTchouIngest "C:\TchouTchou\tchoutchou_ingest\.venv\Scripts\python.exe" "ingest.py --db tchoutchou.db --log-file tchoutchou_ingest.log"
& $nssm set TchouTchouIngest AppDirectory "C:\TchouTchou\tchoutchou_ingest"
& $nssm set TchouTchouIngest AppExit Default Restart
& $nssm set TchouTchouIngest Start SERVICE_AUTO_START
& $nssm start TchouTchouIngest
```

**Daily `aggregate.py` + `purge_raw.py` via Task Scheduler** (wrapped in `.bat` files
since both scripts just use `print()`):

`run_aggregate.bat`:
```bat
@echo off
cd /d C:\TchouTchou\tchoutchou_ingest
.venv\Scripts\python.exe aggregate.py --db tchoutchou.db >> aggregate.log 2>&1
```
`run_purge.bat`:
```bat
@echo off
cd /d C:\TchouTchou\tchoutchou_ingest
.venv\Scripts\python.exe purge_raw.py --db tchoutchou.db --retention-days 5 --vacuum >> purge.log 2>&1
```
```powershell
schtasks /Create /TN "TchouTchou Aggregate" /TR "C:\TchouTchou\tchoutchou_ingest\run_aggregate.bat" /SC DAILY /ST 03:00 /RU SYSTEM
schtasks /Create /TN "TchouTchou Purge" /TR "C:\TchouTchou\tchoutchou_ingest\run_purge.bat" /SC DAILY /ST 03:15 /RU SYSTEM
```
(purge runs 15 minutes after aggregate on purpose — `purge_raw.py` refuses to delete
anything not yet aggregated, so aggregate needs to finish first)

**`--retention-days 5`, not 90, chosen 2026-08-18 under disk pressure**: measured GTFS-RT
raw growth is ~2.2 GB/day (see the platform-layer-redesign section above), and this VPS
had only ~23GB free at the time — roughly 10 days to disk-full at that rate. A 90-day (or
even 14-day) retention window wouldn't start deleting anything until data is that many
days old, which is *after* the disk would fill, since collection had only been running
~1 day. 5 days is close to the minimum workable value (`aggregate.py` needs a trip ≥1 day
old before `purge_raw.py` will touch it, so anything much shorter leaves little margin)
and was chosen to get purging actually happening within the first week. Trade-off: only
5 days of raw history to reprocess a bug from or pull exact percentiles against, down
from the original 90. Revisit upward if/once disk headroom improves (bigger volume) or
`--no-raw` cuts the daily footprint enough to afford a longer window again.

## Next steps, in order

1. Confirm the two Task Scheduler jobs (`TchouTchou Aggregate`, `TchouTchou Purge`) are
   actually created on the VPS — `schtasks /query /TN "TchouTchou Purge"` to check.
   These were still manual as of 2026-08-18; without them the 5-day retention window
   isn't self-enforcing.
2. Finish the SIRI upsert validation (see above) — run `compare_platform_snapshots.py`
   against a same-day, higher-traffic pair of exports, and a few more manual spot-checks
   during daytime service. Once clean, drop SIRI's raw blob (`--no-raw` currently
   applies to all three feeds uniformly — would need a small `ingest.py` change to scope
   it to SIRI only, keeping GTFS-RT's cheaper safety net).
3. Re-check VPS free disk periodically while retention is tight (5 days) — revisit
   raising it once `--no-raw` (SIRI at least) is live and/or disk headroom improves.
4. Let it run **2-4 weeks minimum** before treating any punctuality number as
   meaningful — a day or two of data just checks the pipeline survives unattended, it
   doesn't smooth out one-off disruptions (strikes, engineering works, weather).
5. Once there's real history: add the `station_uic` column to the platform tables,
   cross-validate `product_category_ref` (early signal from spot-checks: UMOL09→RER A,
   164405→Transilien N both look right), and consider the delay-propagation table.
