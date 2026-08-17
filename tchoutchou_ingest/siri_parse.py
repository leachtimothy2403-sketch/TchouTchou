"""
Parses SNCF's SIRI ET (Estimated Timetable) Lite feed -- a different protocol from
GTFS-RT (SIRI is XML, not protobuf), fetched separately by ingest.py's poll_siri_feed().

Built from a real confirmed example (see getplatform.py, the exploration script this
was built from, and README.md's "Platform data" section): SIRI gives the actual
confirmed platform (`ArrivalPlatformName`/`DeparturePlatformName`) once SNCF has
announced it -- something GTFS-RT trip_updates never carries at all.

Each EstimatedVehicleJourney has:
  - identity: TrainNumberRef, PublishedLineName, OriginName, DestinationName,
    ProductCategoryRef (a second, independent train-type signal -- not yet
    cross-validated against parse.py's service_code, worth doing once there's data).
  - a list of calls, each either a RecordedCall (SNCF has confirmed what happened --
    this is where a real platform shows up) or an EstimatedCall (still a prediction,
    not yet confirmed). That recorded-vs-estimated split IS the "Confirmed vs Likely"
    distinction the product wants -- captured directly as `call_type` on each row,
    not inferred.
"""

import re
from datetime import datetime
from typing import Optional

NS = {"siri": "http://www.siri.org.uk/siri"}

_CALL_FIELDS = (
    "StopPointRef", "StopPointName",
    "AimedArrivalTime", "ExpectedArrivalTime", "ArrivalPlatformName",
    "AimedDepartureTime", "ExpectedDepartureTime", "DeparturePlatformName",
)


def xml_to_dict(elem) -> dict:
    """
    Generic, lossless XML->dict conversion (namespace prefixes stripped from tags) --
    the SIRI equivalent of parse.entity_to_dict(). Used to build the raw safety net;
    NOT used for the structured columns below (those use targeted XPath instead, so a
    field showing up somewhere unexpected in the tree doesn't get silently attributed
    to the wrong place).
    """
    children = list(elem)
    if not children:
        return elem.text
    result = {}
    for child in children:
        tag = child.tag.split("}")[-1]
        value = xml_to_dict(child)
        if tag in result:
            if not isinstance(result[tag], list):
                result[tag] = [result[tag]]
            result[tag].append(value)
        else:
            result[tag] = value
    return result


def find_journeys(root):
    return root.findall(".//siri:EstimatedVehicleJourney", NS)


def _find_train_number(journey) -> Optional[str]:
    els = journey.findall(".//siri:TrainNumberRef", NS)
    # In principle SIRI could nest more than one TrainNumberRef under a journey (e.g. a
    # composite/coupled service); taking the first is the practical default -- if that
    # turns out wrong for some journeys, the full list is still visible in raw_gzip via
    # extract_platform_entity.py.
    return els[0].text if els else None


def _parse_calls(journey, tag: str, call_type: str) -> list[dict]:
    calls = []
    for call in journey.findall(f".//siri:{tag}", NS):
        row = {"call_type": call_type}
        for field in _CALL_FIELDS:
            row[field] = call.findtext(f"siri:{field}", namespaces=NS)
        calls.append(row)
    return calls


def parse_journey(journey) -> dict:
    return {
        "train_number": _find_train_number(journey),
        "line_name": journey.findtext("siri:PublishedLineName", namespaces=NS),
        "origin_name": journey.findtext("siri:OriginName", namespaces=NS),
        "destination_name": journey.findtext("siri:DestinationName", namespaces=NS),
        "product_category_ref": journey.findtext("siri:ProductCategoryRef", namespaces=NS),
        "origin_aimed_departure_time": journey.findtext("siri:OriginAimedDepartureTime", namespaces=NS),
        "destination_aimed_arrival_time": journey.findtext("siri:DestinationAimedArrivalTime", namespaces=NS),
        # Recorded first, then estimated -- callers that want "the latest confirmed
        # info first" can just take calls[0] per stop when both exist for that stop.
        "calls": _parse_calls(journey, "RecordedCall", "recorded") + _parse_calls(journey, "EstimatedCall", "estimated"),
    }


_ISO_DATE_RE = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})")


def calendar_date_from_iso(timestamp: Optional[str]) -> Optional[str]:
    """
    'YYYY-MM-DDTHH:MM:SS+02:00' -> 'YYYYMMDD', matching GTFS-RT's start_date format so
    the two feeds are joinable later. Best-effort: SIRI gives no separate "service day"
    field the way GTFS-RT's trip_id does, so this is the practical stand-in -- a train
    just past midnight could in principle land on the "wrong" calendar day relative to
    SNCF's internal service-day convention. Not yet cross-checked against real data.
    """
    if not timestamp:
        return None
    m = _ISO_DATE_RE.match(timestamp)
    return f"{m.group(1)}{m.group(2)}{m.group(3)}" if m else None


def parse_iso_timestamp(timestamp: Optional[str]) -> Optional[datetime]:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except ValueError:
        return None
