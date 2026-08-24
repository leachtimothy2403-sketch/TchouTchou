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

Open `http://localhost:8000/` for the single-train lookup UI, `http://localhost:8000/static/search.html`
for the real journey-search UI (see "SNCF journey search setup" below -- needs `SNCF_API_KEY`),
or hit the JSON endpoints directly, e.g. `http://localhost:8000/api/trains/9575/status`.

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
- `GET /api/search?from=...&to=...&date=YYYY-MM-DD&time=HH:MM` -- real itinerary search
  (added 2026-08-24), backed by SNCF's own journey-planning API and annotated with
  TchouTchou's reliability data per leg, plus a connection-risk-adjusted end-to-end
  probability for itineraries with a change. **Needs `SNCF_API_KEY` set** -- see "SNCF
  journey search setup" below. Backs `static/search.html` (added 2026-08-24), the real
  journey-search UI -- see that section below.

## SNCF journey search setup

`/api/search` calls SNCF's public journey-planning API (Navitia-based) to generate the
actual list of trains between two stations -- TchouTchou's own pipeline tracks trains
that are already running, it doesn't do journey planning, so this piece has to come from
SNCF rather than being built here (see the monetization/MVP discussion,
`tchoutchou_monetization_mvp.md`, for why).

1. Sign up for a free key at
   https://numerique.sncf.com/startup/api/token-developpeur/ -- free tier is 5,000
   requests/day, and data covers roughly yesterday through 23 days ahead.
2. Set it as an environment variable before starting the API:
   `set SNCF_API_KEY=your-key-here` (PowerShell: `$env:SNCF_API_KEY = "your-key-here"`).
3. Try it: `http://localhost:8000/api/search?from=Paris+Gare+de+Lyon&to=Lyon+Part-Dieu&date=2026-08-26&time=18:00`

**Confirmed against a real key 2026-08-24** (Paris Gare de Lyon -> Lyon Part-Dieu): both
previously-unverified assumptions in `sncf_journeys.py` turned out correct --
coverage id `"sncf"` works, and `headsign` holds the plain train number directly (e.g.
`"6669"`). No field-mapping fix was needed. That real response did surface one actual
bug, since fixed -- see "Known gaps" below: an RER/Transilien leg's `headsign` is a
mission code, not a train number, so it never gets reliability history, and a transfer
right after one used to silently show the whole itinerary's combined probability as a
false 100%. It now correctly comes back as unknown (`null`) instead.

**Journey-search API results are cached in-memory for 5 minutes**, bucketed to the
nearest 15-minute departure window, to protect the 5,000/day free tier under repeat
searches for the same popular route/time -- see `sncf_journeys.py`'s
`JOURNEY_CACHE_TTL_SECONDS`. This is per-process and doesn't survive a restart; fine for
a single-instance MVP.

**Commercial-use terms of the SNCF API are not yet confirmed** (see
`tchoutchou_monetization_mvp.md`) -- fine to build and test against, worth confirming with
SNCF (digital@sncf.fr) before this is public-facing.

## Journey-search UI (`static/search.html`)

Added 2026-08-24: a real, working page for `/api/search` -- from/to/date/time inputs,
results sorted by reliability or speed, connecting itineraries expandable to show each
leg and the transfer, and the end-to-end completion probability with its meter. Visually
based on the TrainAware search-results mockup (a Claude Design canvas prototype built
earlier in the same design session -- link in `tchoutchou_monetization_mvp.md`), but that
mockup itself can't be wired to this API directly: it's published as a hosted page on
claude.ai, which runs in a sandboxed iframe with no outbound network access, and
`localhost` isn't reachable from a remote page anyway. So this is a fresh, from-scratch
implementation living in this codebase instead, served by this app and calling `/api/search`
same-origin (no CORS setup needed).

A few real-data details worth knowing:
- For a **direct** train, the card's headline badge is that train's own on-time %.
- For a **connecting** itinerary, the headline badge is `combined_success_probability`
  instead (a different, stricter number -- the odds of completing the whole journey, not
  just one train's punctuality) -- shown as "Unknown" with no fake percentage when it's
  `null` (see the 2026-08-24 bug fix in `main.py`, and the RER/mission-code gap above).
- Platform info isn't shown here -- `/api/search` doesn't return it (that's `/api/trains/{n}/status`,
  a separate lookup); no point fabricating it.
- No build step, no dependencies beyond a Google Fonts `@import` (falls back to system
  fonts if that's unreachable) -- open the file directly or via the FastAPI static mount.

**Verified rendering (2026-08-24) against the real SNCF response captured while building
`/api/search`** (Paris Gare de Lyon → Lyon Part-Dieu, including the RER-into-Chessy
itinerary with the unknown-connection-risk case) using headless Chromium -- all 5 result
cards render correctly, the "Unknown" end-to-end case shows properly (not a false 100%),
and a mocked 502 (missing `SNCF_API_KEY`) renders as a clean error panel. **Not yet
tested with a human clicking through it in a real browser against a live server** -- the
form controls (search toggle, swap, sort, expand/collapse) work in isolation but haven't
been through an actual end-to-end click-through session yet.

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
- **No reliability data for RER/Transilien (mission-code) legs.** Their `headsign` is a
  mission code (e.g. `"QIDO"`), not a numeric train number, so `_extract_train_number()`
  correctly returns `null` for them -- meaning that leg's `reliability` is always `null`,
  and any transfer right after one has unknown connection risk. `combined_success_probability`
  for such itineraries now correctly comes back `null` (see the bug fix above) rather than
  the old false 100%, but there's still no actual reliability number for these legs, since
  this pipeline doesn't have a data source keyed on mission codes today. Closing this would
  mean joining against the SIRI-based mission-code tracking `main.py`'s `/status` endpoint
  already does for platforms -- a real feature, not a quick fix.
- **The end-to-end connection probability is a first-pass estimate**, not the full
  historical buffer-time model from the product design discussion -- it's based on one
  leg's overall delay-bucket history (how often has this train been at least N minutes
  late), not on how the specific pair of trains at this specific transfer have performed
  together, and it doesn't account for correlated delays across legs (e.g. one disruption
  hitting both). Said plainly in `main.py`'s `_connection_success_probability()`
  docstring and in the API response itself isn't currently flagged to the caller beyond
  that -- worth adding a `note` to the top-level `/api/search` response if this ships to
  real users, not just the code comment.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, all endpoints. |
| `db.py` | Read-only connection helper + small utilities duplicated (not imported) from `tchoutchou_ingest/` -- UIC extraction, train nomenclature fallback, mission-code detection. Kept as a separate copy since this is a different deployable that only reads the collector's db. |
| `sncf_journeys.py` | Client for SNCF's journey-planning API (added 2026-08-24) -- station-name resolution, journey search, response parsing. See "SNCF journey search setup" above. |
| `static/index.html` | Self-contained single-page UI (no build step, no external dependencies) -- train number search box that calls the combined endpoint and renders the result. Links to `search.html`. |
| `static/search.html` | Journey-search UI (added 2026-08-24) -- calls `/api/search`, renders sortable/expandable results. See "Journey-search UI" above. |
| `requirements.txt` | `fastapi`, `uvicorn[standard]`, `requests`. |
| `test_search_local.py` | No-network sanity check for `/api/search`'s parsing and connection-risk math, run against a hand-built SNCF-shaped response -- `python test_search_local.py`. Doesn't need `SNCF_API_KEY`; doesn't replace testing against a real response once you have a key. |
