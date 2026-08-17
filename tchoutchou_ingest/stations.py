"""
UIC station code -> name resolution, cache-first, batched.

Backed by SNCF's official "Gares de voyageurs" reference dataset (ODbL), queried via
ressources.data.sncf.com's Explore API using a single batched `in (...)` filter:

    https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/gares-de-voyageurs/records
        ?where=codes_uic in ("87773002","87775007",...)&limit=<batch size>

The first time a given UIC code shows up in the trip_updates feed, it gets queued for
lookup. Every subsequent poll checks the `stations` table (db.py) first, so an unseen
UIC code is looked up exactly once, ever -- and on the first run, a whole batch of
newly-seen codes (a poll of the live feed easily surfaces 50-100+ distinct stations)
goes out as one HTTP call instead of one-per-code. Given France has on the order of a
few thousand stations total, the cache saturates fast: expect a handful of batched
calls on day one and close to nothing after.

'not_found' results are cached too (some UIC codes in the real-time feed may not
appear in the passenger-station reference, e.g. freight-only points or data quirks) so
we don't re-query for a code that will never resolve. A failed batch is NOT cached, so
a transient outage just retries the whole batch on the next poll.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Iterable

import requests

logger = logging.getLogger("tchoutchou.stations")

STATIONS_API = "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/gares-de-voyageurs/records"
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 2
INTER_BATCH_DELAY_SECONDS = 0.3  # be polite -- this is a small reference dataset, not the real-time feed
BATCH_SIZE = 50  # conservative vs. the API's per-request result cap and URL length


def _fetch_batch(uic_codes: list[str]) -> dict[str, dict]:
    """
    Looks up a batch of UIC codes in one request. Returns {uic_code: record} for
    whichever of them were found (codes with no match are simply absent from the dict).
    Raises on network failure after retries.
    """
    quoted = ",".join(f'"{c}"' for c in uic_codes)
    where_clause = f"codes_uic in ({quoted})"

    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                STATIONS_API,
                params={"where": where_clause, "limit": len(uic_codes)},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []
            return {rec["codes_uic"]: rec for rec in results if rec.get("codes_uic")}
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(1.0 * attempt)
    raise last_exc


def _store(cur, uic: str, status: str, record: dict, resolved_at: str):
    pos = (record or {}).get("position_geographique") or {}
    cur.execute(
        "INSERT OR REPLACE INTO stations (codes_uic, nom, libellecourt, segment_drg, lon, lat, "
        "codeinsee, sncf_id, lookup_status, resolved_at_utc, raw_json) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uic,
            record.get("nom"),
            record.get("libellecourt"),
            record.get("segment_drg"),
            pos.get("lon"),
            pos.get("lat"),
            record.get("codeinsee"),
            record.get("id"),
            status,
            resolved_at,
            json.dumps(record, ensure_ascii=False) if record else None,
        ),
    )


def resolve_missing(conn, uic_codes: Iterable[str]) -> int:
    """
    Ensures every UIC code in uic_codes has a row in `stations`, fetching from the API
    in batches of BATCH_SIZE for any that don't yet. Returns how many codes were newly
    resolved (found or confirmed not-found) this call.
    """
    codes = {c for c in uic_codes if c}
    if not codes:
        return 0

    cur = conn.cursor()
    placeholders = ",".join("?" for _ in codes)
    cur.execute(f"SELECT codes_uic FROM stations WHERE codes_uic IN ({placeholders})", tuple(codes))
    already_cached = {row[0] for row in cur.fetchall()}
    missing = sorted(codes - already_cached)
    if not missing:
        return 0

    batches = [missing[i:i + BATCH_SIZE] for i in range(0, len(missing), BATCH_SIZE)]
    logger.info("Resolving %d new UIC code(s) via %d batched API call(s)...", len(missing), len(batches))

    resolved = 0
    for batch in batches:
        try:
            found = _fetch_batch(batch)
        except requests.RequestException as exc:
            logger.warning(
                "Station batch lookup failed for %d code(s): %s (will retry next cycle)", len(batch), exc
            )
            continue  # nothing in this batch gets cached -- retried automatically later

        resolved_at = datetime.now(timezone.utc).isoformat()
        for uic in batch:
            record = found.get(uic)
            _store(cur, uic, "ok" if record else "not_found", record or {}, resolved_at)
            resolved += 1

        conn.commit()
        if len(batches) > 1:
            time.sleep(INTER_BATCH_DELAY_SECONDS)

    logger.info("Resolved %d new UIC code(s) this cycle (%d already cached).", resolved, len(already_cached))
    return resolved
