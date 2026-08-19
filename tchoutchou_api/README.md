# TchouTchou API -- MVP

Read-only product layer on top of `tchoutchou_ingest`'s collector db. Serves the four
features from the original pitch (predictive reliability, live tracking, crowd insights,
deep-link to SNCF Connect) plus a minimal UI to try them.

**Status as of 2026-08-19**: first cut, tested against a real (if partial/sampled) db
snapshot, not yet run against the live VPS db or deployed. See "Known gaps" below before
treating any endpoint as production-ready.

## Run

```
cd tchoutchou_api
pip install -r requirements.txt
set TCHOUTCHOU_DB=..\tchoutchou_ingest\tchoutchou.db      # PowerShell: $env:TCHOUTCHOU_DB = "..\tchoutchou_ingest\tchoutchou.db"
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/` for the UI, or hit the JSON endpoints directly, e.g.
`http://localhost:8000/api/trains/9575/status`.

**Run this next to wherever the live `tchoutchou.db` actually is** -- today that's the
VPS (`C:\TchouTchou\tchoutchou_ingest\tchoutchou.db`), not your local dev machine, since
the API needs live/recent data, not a snapshot. It opens the db read-only (`mode=ro`), so
it's safe to run alongside the `TchouTchouIngest` service without any write conflict.

If you'd rather not open a port on the VPS directly to the internet, put this behind
whatever reverse proxy / tunnel you're already planning for the product (not set up
here -- this MVP is just the app itself).

## Endpoints

- `GET /api/trains/{train_number}/status?date=YYYYMMDD` -- live delay, stop-by-stop
  itinerary, platform (Confirmed/Likely/Unknown) per stop. `date` optional, defaults to
  the most recent date this train has data for.
- `GET /api/trains/{train_number}/reliability` -- punctuality stats from the permanent
  layer (`train_stats`). Returns `available: false` with a reason if `aggregate.py`
  hasn't built history for this train yet.
- `GET /api/trains/{train_number}/crowd` -- **always returns `available: false`** right
  now, see "Known gaps" below. Not a bug.
- `GET /api/trains/{train_number}/deep_link?date=YYYYMMDD` -- best-effort SNCF Connect
  URL, `confirmed: false` until manually validated (see "Known gaps").
- `GET /api/trains/{train_number}` -- all four bundled together (what the UI calls).

## Known gaps (read before demoing this to anyone)

- **Crowd insights has zero underlying data.** Neither GTFS-RT `trip_updates` nor SIRI ET
  Lite carry occupancy/crowding info -- would need a different feed (SIRI-VM's
  `OccupancyRef`, or GTFS-RT `VehiclePositions.occupancy_status`), neither of which
  `ingest.py` polls today. The endpoint is an honest "not available" stub, not a fake
  number -- see its docstring in `main.py`. Closing this gap means adding a new feed to
  the collector first, a bigger piece of work than this API layer.
- **"Live tracking" is per-stop delay predictions, not a moving GPS position.** GTFS-RT
  `trip_updates` doesn't carry vehicle position -- that's a separate feed
  (`VehiclePositions`) not currently polled. The `/status` endpoint's `stops` list is the
  live itinerary instead (which stop's next, how late). If a literal "dot on a map" is
  wanted for v2, that's a new feed + new table, not a UI change on top of what's collected
  now.
- **`stop_sequence` came back NULL for every row checked** in a real sample -- SNCF's
  feed apparently doesn't populate it (optional in the GTFS-RT spec). The API falls back
  to insertion order, which should match the feed's own order, but this is worth a second
  look with more data before trusting stop ordering blindly.
- **SNCF Connect's deep-link URL scheme is unconfirmed.** Built from a plausible guess
  at query params (`origin`, `destination`, `outwardDate`), not verified against the live
  site. Always returns `confirmed: false` so a caller knows to check. Open one in a
  browser and see if it actually prefills the search before shipping this to users.
- **Reliability numbers are provisional.** Collection started 2026-08-17; README.md is
  explicit that 2-4 weeks of history is needed before a punctuality number means
  anything. The endpoint includes `days_of_history` and a `confidence_note` under 14
  days specifically so a caller can decide whether to show the number at all, rather than
  silently trusting it.
- **Only tested against a `monitor_snapshot.py` export so far** (real Aug 18-19 data,
  but `trip_updates` there is a 500-row sample, and the `trains`/`train_stats` permanent
  tables were empty in that export at test time) -- not yet run against the full live
  `tchoutchou.db`. Some paths (e.g. a train with real aggregated reliability history)
  haven't been exercised against real data yet; worth a pass once this is running on the
  VPS.
- **No auth, no rate limiting, no HTTPS.** Fine for local/internal use; needs work before
  this is public-facing.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, all endpoints. |
| `db.py` | Read-only connection helper + small utilities duplicated (not imported) from `tchoutchou_ingest/` -- UIC extraction, train nomenclature fallback, mission-code detection. Kept as a separate copy since this is a different deployable that only reads the collector's db. |
| `static/index.html` | Self-contained single-page UI (no build step, no external dependencies) -- train number search box that calls the combined endpoint and renders the result. |
| `requirements.txt` | `fastapi`, `uvicorn[standard]`. |
