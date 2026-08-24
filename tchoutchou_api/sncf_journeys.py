"""
Client for SNCF's public journey-planning API (Navitia-based; see
https://www.digital.sncf.com/startup/API/ressources). Resolves station names to Navitia
place ids, searches journeys between them, and parses the response into a plain itinerary
shape that main.py's /api/search endpoint annotates with TchouTchou's own reliability
data.

Setup: sign up for a free key at
https://numerique.sncf.com/startup/api/token-developpeur/ (free tier: 5,000 requests/day,
data covers roughly yesterday through 23 days ahead) and set it as the SNCF_API_KEY
environment variable before using /api/search.

IMPORTANT -- what's confirmed vs. inferred (read this before debugging a field-mapping
issue). This module was written from SNCF's/Navitia's PUBLIC documentation
(doc.navitia.io, digital.sncf.com, the SNCFdevelopers integration guide), not from a live
test call -- no API key was available while writing it. Two things specifically are
best-effort, not verified:

  1. The coverage id "sncf" below -- the conventional, documented id for SNCF's national
     rail coverage, but not confirmed by actually calling GET /v1/coverage and checking
     it's in the list.
  2. Where the commercial train number (e.g. "6683") actually lives in a journey's
     display_informations -- several independent Navitia/SNCF integrations put it in
     `headsign`, but this hasn't been checked against a real response for this coverage.
     `_extract_train_number()` below tries a couple of candidate fields; if train numbers
     come back wrong, blank, or as something like "Paris" instead of a number once you
     have a real key, that function is the first place to look. Paste me one real
     /journeys response (or point me at SNCF_API_KEY on your machine) and I'll fix the
     mapping precisely instead of guessing further.

The overall Navitia response shape (journeys/sections/stop_date_times) is long-
established, widely-used public API surface, so that part is on firmer ground than the
two SNCF-specific details above.
"""
import os
import re
import time
from datetime import datetime

import requests

SNCF_API_BASE = "https://api.sncf.com/v1"
COVERAGE = "sncf"
REQUEST_TIMEOUT = 10  # seconds

JOURNEY_CACHE_TTL_SECONDS = 300  # 5 min -- long enough that repeated searches for the
    # same popular route/time share one SNCF API call (protects the 5,000/day free
    # tier), short enough that real-time delay/disruption info doesn't go too stale.
    # In-memory, per-process: fine for a single-instance MVP deployment, but won't
    # survive a restart and won't be shared across multiple uvicorn workers if this ever
    # runs with more than one.

_TRAIN_NUMBER_RE = re.compile(r"\b(\d{2,6})\b")


class SNCFAPIError(Exception):
    pass


def _api_key():
    key = os.environ.get("SNCF_API_KEY")
    if not key:
        raise SNCFAPIError(
            "SNCF_API_KEY is not set. Sign up for a free key at "
            "https://numerique.sncf.com/startup/api/token-developpeur/ and set it as an "
            "environment variable before using /api/search."
        )
    return key


def _get(path, params=None):
    url = f"{SNCF_API_BASE}{path}"
    try:
        # HTTP Basic auth: the token is the username, password is left empty -- see
        # digital.sncf.com's integration guide.
        resp = requests.get(url, params=params, auth=(_api_key(), ""), timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise SNCFAPIError(f"Could not reach the SNCF API: {e}") from e

    if resp.status_code == 401:
        raise SNCFAPIError("SNCF API rejected the key (401) -- check SNCF_API_KEY is correct.")
    if resp.status_code == 429:
        raise SNCFAPIError(
            "SNCF API rate limit hit (429) -- the 5,000/day free-tier quota is likely "
            "exhausted for today."
        )
    if resp.status_code >= 400:
        raise SNCFAPIError(f"SNCF API returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Place resolution -- station name text -> a Navitia place id /journeys can use
# ---------------------------------------------------------------------------

_place_cache = {}  # query (lowercased) -> (place_id, resolved_name)
    # Cached indefinitely per process, separate from the short-lived journey cache below
    # -- station names/ids essentially never change, so there's no reason to burn API
    # quota re-resolving "Paris Gare de Lyon" on every search. Restart the process to
    # pick up a change (there basically never is one).


def resolve_place(query):
    """Resolve free-text station input (e.g. "Paris Gare de Lyon") to a Navitia place id
    usable as `from`/`to` in /journeys, via SNCF's /places autocomplete endpoint."""
    key = query.strip().lower()
    if key in _place_cache:
        return _place_cache[key]

    data = _get(
        f"/coverage/{COVERAGE}/places",
        params={"q": query, "count": 1, "type[]": "stop_area"},
    )
    places = data.get("places") or []
    if not places:
        raise SNCFAPIError(f"No station found for {query!r}.")
    place = places[0]
    result = (place["id"], place.get("name", query))
    _place_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Journey search (cached)
# ---------------------------------------------------------------------------

_journey_cache = {}  # cache_key -> (cached_at_epoch, parsed_journeys)


def _cache_key(origin_query, destination_query, date, time_str):
    # Round the requested time down to a 15-minute bucket so nearby searches for the same
    # route share a cache entry -- e.g. someone searching 18:04 and someone else
    # searching 18:09 both land in the ~18:00 window, which is exactly the "many users
    # searching the same popular route around the same time" case the free tier needs
    # help with.
    hh, mm = time_str.split(":")
    bucket = (int(mm) // 15) * 15
    return (origin_query.strip().lower(), destination_query.strip().lower(), date, f"{hh}:{bucket:02d}")


def search_journeys(origin_query, destination_query, date, time_str, count=5):
    """
    date: "YYYY-MM-DD" (or "YYYYMMDD")
    time_str: "HH:MM", interpreted as the requested departure time.
    Returns a list of parsed itineraries (see _parse_journey), in the order SNCF returns
    them -- main.py re-ranks by reliability, this just returns what SNCF gave back.
    """
    key = _cache_key(origin_query, destination_query, date, time_str)
    cached = _journey_cache.get(key)
    if cached and (time.time() - cached[0]) < JOURNEY_CACHE_TTL_SECONDS:
        return cached[1]

    origin_id, origin_name = resolve_place(origin_query)
    dest_id, dest_name = resolve_place(destination_query)

    data = _get(
        f"/coverage/{COVERAGE}/journeys",
        params={
            "from": origin_id,
            "to": dest_id,
            "datetime": _format_datetime(date, time_str),
            "datetime_represents": "departure",
            "count": count,
            "data_freshness": "realtime",
        },
    )
    journeys = [_parse_journey(j, origin_name, dest_name) for j in (data.get("journeys") or [])]

    _journey_cache[key] = (time.time(), journeys)
    return journeys


def _format_datetime(date, time_str):
    d = date.replace("-", "")
    t = time_str.replace(":", "")
    if len(t) == 4:
        t += "00"
    return f"{d}T{t}"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_train_number(display_informations):
    """See module docstring -- inferred, not verified against a real response. Tries
    `headsign` first (commonly holds the commercial train number for long-distance SNCF
    services in other Navitia/SNCF integrations), then `code`, then a last-resort digit
    scrape of headsign."""
    for field in ("headsign", "code"):
        val = (display_informations.get(field) or "").strip()
        if val and _TRAIN_NUMBER_RE.fullmatch(val):
            return val
    headsign = display_informations.get("headsign") or ""
    m = _TRAIN_NUMBER_RE.search(headsign)
    return m.group(1) if m else None


def _parse_journey(journey, origin_name, destination_name):
    legs = []
    for section in journey.get("sections", []):
        if section.get("type") != "public_transport":
            continue  # skip walking/waiting connector sections -- transfers below are
                       # derived from the gap between consecutive train legs instead
        di = section.get("display_informations", {}) or {}
        legs.append({
            "train_number": _extract_train_number(di),
            "operator": di.get("network"),
            "physical_mode": di.get("physical_mode"),
            "headsign": di.get("headsign"),
            "from_station": (section.get("from") or {}).get("name"),
            "to_station": (section.get("to") or {}).get("name"),
            "departure_datetime": section.get("departure_date_time"),
            "arrival_datetime": section.get("arrival_date_time"),
            "duration_seconds": section.get("duration"),
        })

    transfers = []
    for i in range(len(legs) - 1):
        gap = _minutes_between(legs[i]["arrival_datetime"], legs[i + 1]["departure_datetime"])
        transfers.append({"at_station": legs[i]["to_station"], "buffer_minutes": gap})

    return {
        "origin": origin_name,
        "destination": destination_name,
        "departure_datetime": journey.get("departure_date_time"),
        "arrival_datetime": journey.get("arrival_date_time"),
        "duration_seconds": journey.get("duration"),
        "nb_transfers": journey.get("nb_transfers"),
        "status": journey.get("status") or None,  # e.g. SIGNIFICANT_DELAYS, NO_SERVICE
        "legs": legs,
        "transfers": transfers,
    }


def _minutes_between(iso_a, iso_b):
    if not iso_a or not iso_b:
        return None
    fmt = "%Y%m%dT%H%M%S"
    a = datetime.strptime(iso_a, fmt)
    b = datetime.strptime(iso_b, fmt)
    return round((b - a).total_seconds() / 60)
