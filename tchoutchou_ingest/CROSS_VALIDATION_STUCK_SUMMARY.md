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

## Outside advice received + validated against real data (2026-08-19)

Got outside advice recommending exactly this: stop iterating on join-key theories, and
instead split SIRI's `train_number` values into three structurally different buckets
before computing any match rate —

- **A. Direct numeric match** (SIRI `164405` vs GTFS-RT `164405`) — the only bucket where
  an unmatched pair is a genuine mystery.
- **B. Coupled-unit pairs** (`"126682-126683"`) — untested until now: match if *either*
  half is in GTFS-RT, instead of treating the whole string as unmatchable.
- **C. Alphanumeric mission codes** (`"UMOL09"`) — structurally can never match, not a bug.

Then, for bucket A, compare `product_category_ref` distribution between matched and
unmatched trains — a scope/semantics mismatch would show up as unmatched trains skewing
toward regional/suburban categories, independent of any join-key bug.

**New script**: `diagnose_feed_scope.py` implements this (buckets A/B/C, matched-vs-
unmatched category comparison, top `line_name` values per bucket, and a `--window` option
to pull SIRI journeys from neighboring calendar dates too, addressing the advice's point
about checking contemporaneity without requiring the same poll timestamp on both sides).

**Caveat on what was actually run**: the production VPS db (the one that produced this
doc's 262/1,106/10,425 figures, accumulated over several days) wasn't available from this
session — only local dev/test `.db` files were. The most useful one, `demo.db`, is a
**single real day** (2026-08-17) of live-polled data, predating the 2026-08-18
platform-layer schema migration (its `platform_journeys` is still the old append-only,
one-row-per-poll design, not the new upsert-to-final-value one — the script dedupes
defensively but this is worth knowing). So the *numbers* below are not directly
comparable to the doc's headline figures and don't replace re-running this against the
live VPS db. The *pattern* is what's informative.

**Result, run against `demo.db` for 2026-08-17** (`python diagnose_feed_scope.py --db
demo.db --window 0`):

- Bucket A: 2,514/3,561 numeric trains matched directly (70.6%) — much higher than the
  23.7% cited above. This alone suggests the production run's low rate isn't purely
  structural (a single clean day joins far better), though it doesn't pin down why the
  multi-day VPS number is lower — worth re-running this script against the live db to see
  if the pattern holds or if something else is going on over longer windows.
- The scope signature the advice predicted showed up clearly: matched numeric trains are
  63.5% `regionalRail`, 9.4% `highSpeedRail`, 8.7% `suburbanRailway`; **unmatched** numeric
  trains are 43.3% `local` + 40.5% `suburbanRailway` (84% combined) vs. 8.8% combined in
  the matched set. Top `line_name` values for the unmatched bucket are almost entirely
  single-letter Transilien codes (E, H, L, J, C, D, P — plus a few TER-style regional
  routes). **Bucket C (alphanumeric mission codes) is 100% `suburbanRailway`, all
  `line_name` A or B — i.e. RER A/B.**
- Conclusion: the gap is overwhelmingly a **feed-scope/semantics difference (advice's A +
  C), not a broken join** — this GTFS-RT proxy carries little of the Transilien/RER
  suburban network that SIRI covers in depth, and that network partly uses
  alphanumeric mission codes (RER A/B) that can't join at all even in principle.
- Side finding while at it: cross-checking GTFS-RT's unmapped `service_code`s the same
  way — `NA` (i.e. no service_code segment in `trip_id` at all) matched SIRI 283/311 times
  (91%), and of those, 77% were `suburbanRailway`, with the rest split across
  `regionalCoach`/`railReplacementCoach`/`highSpeedRail`/`international`. So `NA` isn't a
  single clean category the way `ICN` is (16/16 `longDistance` in the original doc, 9/9
  here) — it looks like "no distinct commercial brand in the trip_id," a grab-bag skewed
  toward Transilien-adjacent + coach services, not one train_type. Recommend **not**
  forcing `NA` into a single `parse.py` label; `ICN` → "Intercités de Nuit" still looks
  solid and safe to add. `TRN` sample was too small here (n=2, both `longDistance`,
  consistent with the doc's existing lean but not enough to confirm on its own).

**Recommendation**: park the deep join-mechanics chase, per the advice's framing — this
looks like A (scope) + C (semantics), not B (a bug). Concrete next actions instead of more
join-key iteration:
1. Add `ICN` → "Intercités de Nuit" to `parse.py` (already had 16/16, now 9/9 elsewhere —
   solid).
2. Add either-half matching for coupled-unit pairs to `cross_validate_train_type.py` (cheap,
   was untested — bucket B here matched 26/80, i.e. it's a real, if modest, win).
3. Re-run `diagnose_feed_scope.py --window 1` against the live VPS db once convenient, to
   confirm the same pattern holds at production scale and to see whether the 23.7% vs
   70.6% match-rate gap between the two runs is itself telling (e.g. does match rate drop
   as the SIRI-side date range widens, suggesting something date/staleness-related after
   all?) — but this is a nice-to-have, not a blocker.
4. Leave `TRN`/`NA` unresolved rather than guessing further; there's no MVP dependency on
   them (see original doc's point 7 above), and `NA` in particular doesn't look like it
   decodes to one clean label no matter how much more data arrives.
