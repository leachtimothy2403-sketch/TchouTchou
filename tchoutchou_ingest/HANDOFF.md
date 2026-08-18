# TchouTchou — Data Pipeline Handoff

Status as of 2026-08-17. This covers the SNCF data collection pipeline built this
session: what exists, what's been validated against real data, what's still open, and
how to pick it up on the VPS.

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
  every GTFS-RT poll, kept for a retention window (default 90 days), then purged by
  `purge_raw.py` once folded into the permanent layer. Retained on a window (rather than
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

Repo target: `https://github.com/leachtimothy2403-sketch/TchouTchou.git`

The repo could **not** be initialized from within this session — the connected Windows
folder is mounted through a bridge that doesn't support file deletion, and git's
internal locking needs that. A `.gitignore` was created directly in
`C:\Users\leach\tchoutchou\.gitignore` (excludes `*.db`, `*.log`, `__pycache__/`, and
the large SNCF static-schedule export). **You still need to run `git init` yourself**
on your Windows machine — see the commands from earlier in this conversation, or below.

```powershell
cd C:\Users\leach\tchoutchou
Remove-Item -Recurse -Force .git   # clean up the broken partial init, if present
git init
git add -A
git commit -m "Initial commit: TchouTchou SNCF ingestion pipeline"
git branch -M main
git remote add origin https://github.com/leachtimothy2403-sketch/TchouTchou.git
git push -u origin main
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
python purge_raw.py --db tchoutchou.db --retention-days 90

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
.venv\Scripts\python.exe purge_raw.py --db tchoutchou.db --retention-days 90 --vacuum >> purge.log 2>&1
```
```powershell
schtasks /Create /TN "TchouTchou Aggregate" /TR "C:\TchouTchou\tchoutchou_ingest\run_aggregate.bat" /SC DAILY /ST 03:00 /RU SYSTEM
schtasks /Create /TN "TchouTchou Purge" /TR "C:\TchouTchou\tchoutchou_ingest\run_purge.bat" /SC DAILY /ST 03:15 /RU SYSTEM
```
(purge runs 15 minutes after aggregate on purpose — `purge_raw.py` refuses to delete
anything not yet aggregated, so aggregate needs to finish first)

## Next steps, in order

1. Run the 2-hour VPS test above.
2. Install the NSSM service and the two scheduled tasks.
3. Let it run **2-4 weeks minimum** before treating any punctuality number as
   meaningful — a day or two of data just checks the pipeline survives unattended, it
   doesn't smooth out one-off disruptions (strikes, engineering works, weather).
4. `git init`/push from your Windows machine (not from this session — see above).
5. Once there's real history: add the `station_uic` column to the platform tables,
   cross-validate `product_category_ref`, and consider the delay-propagation table.
