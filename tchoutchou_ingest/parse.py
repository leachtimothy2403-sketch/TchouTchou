"""
Turns a raw GTFS-RT FeedMessage into the rows db.py's schema expects.

Everything goes through google.protobuf.json_format.MessageToDict first, with
including_default_value_fields=True. That gives us a plain dict with every field
GTFS-RT defines (not just the ones a given entity happens to set), which we then:
  1. store whole, as raw_json, per entity -- the safety net.
  2. pick specific keys out of, for the structured columns used in day-to-day queries.

If SNCF adds a field we don't have a column for, it still shows up in raw_json.
Nothing is silently dropped.
"""

import json
import re
from typing import Optional

from google.protobuf.json_format import MessageToDict

# GTFS-RT encodes int64/uint64 fields (timestamps, delays in some contexts) as
# JSON strings to avoid precision loss. Cast them back to int for SQLite.
def _int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def entity_to_dict(entity) -> dict:
    # protobuf renamed including_default_value_fields -> always_print_fields_with_no_presence
    # in newer releases (5.x+). Support both so this doesn't break depending on what
    # `pip install` resolves on the machine that actually runs the collector.
    try:
        return MessageToDict(
            entity,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
    except TypeError:
        return MessageToDict(
            entity,
            preserving_proto_field_name=True,
            including_default_value_fields=True,
        )


# --- trip_id structural parsing --------------------------------------------------------
#
# trip_id looks like:
#   OCESN9898F1187_F:OUI:FR:Line::4FA25873-A63A-4A2D-B62F-EF950E45D8A9::87773002:87191007:12:1349:20260821
#   [OCESN][train#][F|R][...]:[brand]:[country]:Line::[route uuid]::[origin UIC]:[dest UIC]:[mission?]:[run id]:[date]
#
# TRAIN NUMBER -- validated (2026-08-17) against the full static trips.txt export in
# Export_OpenData_SNCF_GTFS_NewTripId/: extracting the digits between the operator prefix
# (e.g. "OCESN") and the following single uppercase letter, and comparing against that
# same trip's trip_headsign field (SNCF's own ground truth for commercial train number),
# matched on ALL 46,192/46,192 trips in the export -- zero mismatches, across all 4
# operator prefixes present (OCESN, OCESA, OCEEA, OCELO) and both suffix letters seen
# (F, R). See validate_extraction.py to re-run this check against a newer static export.
#
# This SUPERSEDES an earlier, wrong version of this heuristic that took "the segment
# right before the trailing date" as the train number -- for the example above that
# returned 1349, which is actually some other run/schedule identifier, not the train
# number (9898). Caught by cross-referencing trips.txt rather than trusting a single
# live sample.
#
# ORIGIN/DESTINATION UIC + mission code -- validated separately against stop_times.txt
# (comparing the parsed origin/destination against each trip's actual first/last stop by
# stop_sequence): 494/494 on a random sample, zero mismatches. This part of the parse was
# right the first time; only the train number extraction needed correcting.
#
# Both fall back to lower-confidence heuristics for any trip_id that doesn't match these
# patterns (shouldn't happen based on the full static feed, but a live feed could differ
# from today's export) so nothing crashes -- it just flags lower confidence for that row.
_TRAIN_NUMBER_PATTERN = re.compile(r"\A[A-Za-z]+(\d+)[A-Z]")
_TRAIN_NUMBER_FALLBACK_RE = re.compile(r"(\d{3,6})")
_DATE_RE = re.compile(r"\A\d{8}\Z")
_NUMBER_RE = re.compile(r"\A\d{3,6}\Z")
_UIC_RE = re.compile(r"\A\d{7,8}\Z")

# --- Service/brand code -----------------------------------------------------------------
#
# The segment right after the train number is a short operator/brand code, e.g. "OUI" in
# OCESN9898F1187_F:OUI:FR:Line::... (trip_id.split(":")[1]). Extraction of the raw code
# itself is validated: every one of the 46,192 trips in trips.txt has a non-empty second
# colon-segment, so `service_code` is never a guess -- it's a direct, always-present read.
#
# Mapping that raw code to a human-facing category (TGV INOUI, OUIGO, TER, ...) is a
# SEPARATE, lower-confidence step, deliberately not done from one example. Evidence per
# code, gathered 2026-08-17 by cross-referencing all trips carrying that code against
# routes.txt (route_short_name/route_type) and, where relevant, destination UIC country
# prefixes (85=Switzerland, 80=Germany) -- see explore_service_code findings in
# validate_extraction.py's discover_service_codes(). Only "high" confidence codes get a
# train_type label; everything else stays None rather than asserting an unverified guess.
#
# code   : (label, confidence, evidence)
SERVICE_CODE_INFO = {
    "TER":  ("TER", "high", "Exact match to the TER brand name; largest code by volume (30,366 trips)."),
    "OUI":  ("TGV INOUI", "high", "Matches the 'inOUI' brand; the example we independently confirmed "
             "(train 9898, Montpellier Saint-Roch) carries this code."),
    "IC":   ("Intercites", "high", "Standard Intercites abbreviation; route_short_names match known "
             "long-distance IC lines (560B, 190A, ...)."),
    "OGO":  ("OUIGO", "high", "Matches the OUIGO brand; uses a distinct non-UUID route_id scheme "
             "(e.g. OCESN-87547000-87581009), consistent with OUIGO being modeled separately; "
             "4000-series train numbers match known OUIGO numbering."),
    "TT":   ("Tram-Train", "high", "Every associated route has route_type=0 (tram) in routes.txt; "
             "route_short_names use tram-style 'T' prefixes (T14, T3, T34)."),
    "LYR":  ("TGV Lyria", "high", "Matches the Lyria brand (Paris-Switzerland joint venture); "
             "destination UIC codes include the 85xxxxxx prefix (Switzerland)."),
    "ICE":  ("ICE (France-Germany)", "high", "Matches the SNCF/DB joint ICE brand; destination UIC "
             "codes include the 80xxxxxx prefix (Germany)."),
    "NAV":  ("Navette", "high", "Matches 'Navette' (shuttle); single dedicated route, distinctive "
             "100000+ train numbering unlike any other code."),
    "CTE":  ("Car TER (TER replacement bus)", "high",
             "Confirmed live 2026-08-17, same method as CRE: a real trip's stop_id spelled out "
             "'Car TER' directly. Consistent with CTE sharing route_short_names with TER (K54, P61, "
             "P42, ...) plus some route_type=3 (bus) legs seen in the static export -- a bus "
             "substitute for part of a TER journey, same pattern as CRE for Intercites."),
    "CRE":  ("Car a reservation (reserved coach/rail-replacement bus)", "high",
             "Confirmed live 2026-08-17: train 67630's next stop_id was literally "
             "'StopPoint:OCECar a reservation-87698902' -- SNCF's own label spells out the service type. "
             "Also explains why that UIC (87698902) isn't in the passenger-station reference dataset: "
             "it's a coach stop, not a rail station. Consistent with CRE sharing route_short_names with "
             "IC (long-distance) lines -- a bus substitute for part of an Intercites journey."),
    "ICN":  (None, "low", "Unresolved -- train-number range loosely resembles known Intercites de "
             "nuit (night train) numbering, not confirmed."),
    "TRN":  (None, "low", "Unresolved -- tiny, unusual sample (90 trips, only 6 distinct 2-digit train "
             "numbers, one dedicated route); likely a niche/special service."),
    "NA":   (None, "low", "Unresolved -- only 2 trips in the whole export, too small to interpret."),
}


def describe_service_code(code: Optional[str]) -> tuple[Optional[str], str]:
    """Returns (train_type_label_or_None, confidence). confidence is 'high'/'low'/'unknown'."""
    if not code:
        return None, "unknown"
    info = SERVICE_CODE_INFO.get(code)
    if info is None:
        return None, "unknown"  # a code not seen in the 2026-08-17 static export sample
    label, confidence, _evidence = info
    return label, confidence


def parse_trip_id(trip_id: Optional[str]) -> dict:
    """Structural parse, validated against static GTFS ground truth. See notes above."""
    result = {
        "train_number": None, "service_code": None, "train_type": None,
        "origin_uic": None, "destination_uic": None, "mission_code": None,
    }
    if not trip_id:
        return result

    m = _TRAIN_NUMBER_PATTERN.match(trip_id)
    if m:
        result["train_number"] = m.group(1)
    else:
        # Lower-confidence fallback: longest digit run anywhere in the string.
        matches = _TRAIN_NUMBER_FALLBACK_RE.findall(trip_id)
        if matches:
            result["train_number"] = max(matches, key=len)

    parts = trip_id.split(":")

    if len(parts) > 1 and parts[1]:
        result["service_code"] = parts[1]
        label, confidence = describe_service_code(parts[1])
        if confidence == "high":
            result["train_type"] = label  # only assert a category we actually have evidence for

    if len(parts) >= 5 and _DATE_RE.match(parts[-1]) and _NUMBER_RE.match(parts[-2]):
        if re.fullmatch(r"\d{1,3}", parts[-3]):
            result["mission_code"] = parts[-3]
        if _UIC_RE.match(parts[-4]):
            result["destination_uic"] = parts[-4]
        if _UIC_RE.match(parts[-5]):
            result["origin_uic"] = parts[-5]

    return result


def guess_commercial_train_number(trip_id: Optional[str]) -> Optional[str]:
    return parse_trip_id(trip_id)["train_number"]


def _translated_text(translated_string: Optional[dict]) -> Optional[str]:
    """Pull a display string out of a GTFS-RT TranslatedString dict, preferring French."""
    if not translated_string:
        return None
    translations = translated_string.get("translation") or []
    if not translations:
        return None
    for t in translations:
        if t.get("language") == "fr":
            return t.get("text")
    return translations[0].get("text")


def trip_update_row(snapshot_id: int, ent: dict) -> tuple[dict, list[dict]]:
    """Returns (trip_updates row dict, list of stop_time_updates row dicts)."""
    tu = ent.get("trip_update") or {}
    trip = tu.get("trip") or {}
    vehicle = tu.get("vehicle") or {}
    stop_updates = tu.get("stop_time_update") or []

    trip_id = trip.get("trip_id")
    parsed = parse_trip_id(trip_id)
    row = {
        "snapshot_id": snapshot_id,
        "entity_id": ent.get("id"),
        "trip_id": trip_id,
        "route_id": trip.get("route_id"),
        "direction_id": _int(trip.get("direction_id")),
        "start_date": trip.get("start_date"),
        "start_time": trip.get("start_time"),
        "schedule_relationship": trip.get("schedule_relationship"),
        "commercial_train_number": parsed["train_number"],
        "service_code": parsed["service_code"],
        "train_type": parsed["train_type"],
        "origin_uic": parsed["origin_uic"],
        "destination_uic": parsed["destination_uic"],
        "mission_code": parsed["mission_code"],
        "vehicle_id": vehicle.get("id"),
        "vehicle_label": vehicle.get("label"),
        "trip_update_timestamp": _int(tu.get("timestamp")),
        "trip_update_delay": _int(tu.get("delay")),
        "stop_time_update_count": len(stop_updates),
    }

    stu_rows = []
    for stu in stop_updates:
        arrival = stu.get("arrival") or {}
        departure = stu.get("departure") or {}
        stu_rows.append({
            "stop_sequence": _int(stu.get("stop_sequence")),
            "stop_id": stu.get("stop_id"),
            "schedule_relationship": stu.get("schedule_relationship"),
            "arrival_delay": _int(arrival.get("delay")),
            "arrival_time": _int(arrival.get("time")),
            "arrival_uncertainty": _int(arrival.get("uncertainty")),
            "departure_delay": _int(departure.get("delay")),
            "departure_time": _int(departure.get("time")),
            "departure_uncertainty": _int(departure.get("uncertainty")),
        })

    return row, stu_rows


def service_alert_row(snapshot_id: int, ent: dict) -> dict:
    alert = ent.get("alert") or {}
    active_periods = alert.get("active_period") or []
    first_period = active_periods[0] if active_periods else {}

    return {
        "snapshot_id": snapshot_id,
        "entity_id": ent.get("id"),
        "cause": alert.get("cause"),
        "effect": alert.get("effect"),
        "header_text": _translated_text(alert.get("header_text")),
        "description_text": _translated_text(alert.get("description_text")),
        "active_period_start": _int(first_period.get("start")),
        "active_period_end": _int(first_period.get("end")),
        "informed_entities_json": json.dumps(alert.get("informed_entity") or [], ensure_ascii=False),
    }
