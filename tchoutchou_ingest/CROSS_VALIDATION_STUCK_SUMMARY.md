# TchouTchou — Train-Type Cross-Validation: Where We're Stuck

## Project context

TchouTchou is a French train intelligence platform (predictive reliability, live
tracking, deep-linking to SNCF Connect). Currently in an early data-collection phase: a
Python collector (`ingest.py`) runs continuously on a Windows VPS, polling two
independent SNCF real-time feeds into a SQLite db.

## The two feeds involved

**GTFS-RT** (protobuf, polled every 2 min, national coverage) → `trip_updates` table.
Train identity: `commercial_train_number`, parsed from the feed's `trip_id` via a regex
in `parse.py`. **Confirmed always purely numeric** — 0 of 10,425 distinct values contain
a non-digit character. Each row also has `service_code` (a raw operator/brand code, e.g.
`"OUI"`) mapped via a lookup table in `parse.py` to a human-readable `train_type`. Some
codes are mapped with high confidence (TER, TGV INOUI, OUIGO, Intercités, TGV Lyria, ICE,
Navette, Car TER, Car à réservation); three are not (`ICN`, `TRN`, `NA`).

**SIRI ET Lite** (XML, separate poll) → `platform_journeys`/`platform_calls` tables
(recently redesigned to UPSERT-to-final-value rather than append-only history — not
directly relevant to this problem, mentioned for completeness). Train identity:
`train_number` (from SIRI's `TrainNumberRef`). This field is **not uniformly formatted**:
sometimes plain numeric, sometimes a hyphenated pair of two plain numbers (e.g.
`"126682-126683"`, apparently SNCF's convention for coupled train units sharing one
journey entry), sometimes a true alphanumeric "mission code" (e.g. `"UMOL09"`, the
RER/Transilien style). SIRI also carries `product_category_ref` (a richer 12-value enum:
`highSpeedRail`, `regionalRail`, `suburbanRailway`, `tramTrain`, `longDistance`, etc.) and
`line_name` (human-readable, e.g. `"RER Ligne A"`, `"Transilien Ligne N"`, or a route
description like `"Grasse - Cannes - Nice - Vintimille"` for TER lines).

## What we're trying to do

Join `trip_updates` and `platform_journeys` on train number (+ date) to see, from real
traffic, what `line_name`/`product_category_ref` each GTFS-RT `service_code` actually
corresponds to. Two goals:

1. **Decode the three unmapped `service_code`s** (`ICN`, `TRN`, `NA`) using SIRI's
   `line_name`/`product_category_ref` as ground truth, instead of guessing.
2. **Sanity-check the already-mapped codes** — a single `service_code` showing several
   unrelated `product_category_ref` values across many trains would suggest the mapping
   is wrong for some slice of trains, not just incomplete.

This was triggered by a manual spot-check (done for an unrelated reason — validating a
different part of the pipeline) that happened to identify two real trains: `UMOL09` =
RER Ligne A, and `164405` = Transilien Ligne N. That suggested the unmapped codes might
decode cleanly if cross-referenced against `line_name`.

## What we've tried, and what we found

**Attempt 1 — strict join** (`trip_updates.start_date = platform_journeys.calendar_date`):
matched only 262 of 10,425 distinct GTFS-RT train numbers (2.5%). Also built a "flag
service codes with many line_name variants" consistency check, which turned out to flag
the wrong thing — `line_name` legitimately varies a lot for a broad category like TER
(which spans dozens of real regional lines), so that's not a useful inconsistency signal.
Fixed to flag `product_category_ref` diversity instead, which is meaningful (a
service_code should have one dominant category even when line names vary).

**Attempt 2 — midnight-crossing date hypothesis**: GTFS's `start_date` convention keeps a
trip's *original* service day even when its stops fall after midnight (a train departing
23:50, arriving 01:30, keeps yesterday's `start_date`), while SIRI's `calendar_date`
likely reflects the real calendar date. Both known spot-checked trains arrive just after
midnight (01:30, 01:32) — exactly this failure mode. Added a loose-date option (also try
`calendar_date = start_date + 1 day`).

**Result: match count barely moved** (262 → 262/263). This hypothesis was wrong, or at
least not the dominant cause. Not yet replaced with a better one.

**Attempt 3 — check the real denominator**: `platform_journeys` has only 1,106 distinct
`train_number` values total, vs. 10,425 in `trip_updates`. SIRI simply covers a much
smaller universe of trains. Against the correct ceiling, the match rate is really
262/1,106 = 23.7%, not 2.5% — better framing, but still leaves ~76% of SIRI's own trains
unmatched, unexplained.

**Attempt 4 — non-numeric train_number check**: of `platform_journeys`'s 1,106 trains,
143 (12.9%) have a non-numeric `train_number`. Since `trip_updates.commercial_train_number`
is confirmed to never contain a non-digit character, these 143 can only match GTFS-RT if
GTFS-RT is *also* sometimes non-numeric, which it isn't — so at face value, none of these
143 should ever match. But this bucket turned out to mix two different things:
- Coupled-unit pairs like `"126682-126683"` (digits-hyphen-digits) — potentially fixable
  by splitting on the hyphen and matching either half.
- True alphanumeric mission codes like `"UMOL09"` — structurally unmatchable, a genuine
  scope difference between the feeds, not a bug.

  A script to split these two apart and count each was just written
  (`check_mission_codes.py`) but results aren't in yet.

## Where we're actually stuck

Even in the best case — all 143 non-numeric trains excluded — that only lowers the
ceiling to ~963. The actual match count (262) is still less than a third of that reduced
ceiling. **We have not yet diagnosed why several hundred trains with plain, presumably
compatible numeric IDs in both feeds still fail to join.** A small manual sample of both
ID formats looked compatible (no obvious leading-zero or casing mismatch), but that was
only ~15 values per side, not a rigorous check. Untested hypotheses:
- SIRI ET Lite may have a real, narrower geographic/operational scope than the specific
  GTFS-RT proxy being polled (different national coverage), independent of any join bug.
- A subtler format issue not visible in a small manual sample (whitespace, encoding,
  occasional padding).
- Some other date/window mismatch distinct from the midnight-crossing one already ruled
  out.

We've spent several iterations narrowing the join-mechanics problem (date logic, format,
correct denominator) without reaching a root cause for the majority of the gap yet.

## What's usable regardless of resolving this

- `ICN` decodes cleanly already: 16/16 matched trains are `longDistance`, suggesting
  "Intercités de Nuit" (night trains) with reasonably high confidence, even on a small
  sample.
- Already-mapped codes `TER`, `CTE`, `IC` show a single dominant `product_category_ref`
  each — those mappings look solid.
- `OUI` (TGV INOUI) and `OGO` (OUIGO) both show a `product_category_ref` split
  (highSpeedRail vs. local, etc.) that's plausibly *correct* rather than a data-quality
  bug — OUIGO genuinely has two sub-brands (Grande Vitesse / Train Classique), and some
  INOUI legs may run on conventional track.
- `TRN`/`NA` (the codes we most wanted to decode, suspected RER/Transilien) still don't
  have enough matched samples to confirm anything — likely because RER/Transilien
  services are exactly the ones using alphanumeric mission codes that can't join at all.

## What we'd like advice on

Whether there's a smarter way to root-cause a cross-feed join mismatch like this than
iterating on join-key theories one at a time, whether it's worth checking SIRI ET Lite's
actual documented scope/coverage before assuming it's a join bug, and — given this is a
secondary validation/enrichment effort rather than something blocking the MVP — whether
it's worth continuing to chase right now vs. parking it.
