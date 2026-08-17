#!/usr/bin/env python3
"""
One-shot: for each service_code that parse.py couldn't confidently map to a train_type
(CTE, CRE, ICN, TRN, NA -- or any other 'low'/'unknown' confidence code currently in the
feed), find one live example and print its train number, service code, and next stop
(resolved to a station name where possible). Meant to be pasted back into a conversation,
or looked up directly on SNCF Connect / a live train tracker, to nail down what each code
actually means.

Usage:
    python find_examples.py
    python find_examples.py --codes CTE,ICN     # just a subset
"""

import argparse
import sys
import time
from datetime import datetime

import requests
from google.transit import gtfs_realtime_pb2

import parse
import stations
from ingest import FEEDS


def _next_stop(stop_updates, now_ts):
    """
    Returns (stop_time_update, is_future). GTFS-RT stop_time_update lists can include
    stops already passed (not just remaining ones), so picking index 0 is wrong -- have
    to find the first entry whose arrival/departure time is still ahead of now.
    Falls back to the last entry (marked is_future=False) if the whole trip is in the past.
    """
    if not stop_updates:
        return None, False
    for stu in stop_updates:
        t = stu.get("arrival", {}).get("time") or stu.get("departure", {}).get("time")
        if t and int(t) >= now_ts:
            return stu, True
    return stop_updates[-1], False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None,
                     help="Comma-separated codes to look for (default: all 'low'/unmapped confidence codes)")
    args = ap.parse_args()

    if args.codes:
        target_codes = [c.strip() for c in args.codes.split(",")]
    else:
        target_codes = [
            code for code, (label, confidence, _) in parse.SERVICE_CODE_INFO.items()
            if confidence != "high"
        ]

    print(f"Looking for one live example of each: {target_codes}")
    print(f"Fetching {FEEDS['trip_updates']} ...")
    resp = requests.get(FEEDS["trip_updates"], timeout=30)
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    now_ts = feed.header.timestamp or int(time.time())
    print(f"entities in feed: {len(feed.entity)}\n")

    # For each code, prefer a trip that still has a stop ahead of it over one that's
    # already finished -- keep scanning until every code has a "future" example, or
    # we run out of entities.
    found = {}
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        ent = parse.entity_to_dict(entity)
        trip = ent.get("trip_update", {}).get("trip", {})
        trip_id = trip.get("trip_id")
        parsed = parse.parse_trip_id(trip_id)
        code = parsed["service_code"]
        if code not in target_codes:
            continue

        stop_updates = ent.get("trip_update", {}).get("stop_time_update", [])
        nxt, is_future = _next_stop(stop_updates, now_ts)

        existing = found.get(code)
        if existing is None or (not existing[3] and is_future):
            found[code] = (trip_id, parsed, nxt, is_future)

        if len(found) == len(target_codes) and all(v[3] for v in found.values()):
            break

    missing = set(target_codes) - set(found.keys())
    if missing:
        print(f"Not found in this poll (try again, or a different time of day): {sorted(missing)}\n")

    # Resolve next-stop UICs to station names in one batch, best-effort.
    next_stop_uics = set()
    for trip_id, parsed, nxt, is_future in found.values():
        if nxt:
            stop_id = nxt.get("stop_id", "")
            m = parse.re.search(r"(\d{5,8})\Z", stop_id)
            if m:
                next_stop_uics.add(m.group(1))
    station_names = {}
    if next_stop_uics:
        try:
            station_names = stations._fetch_batch(sorted(next_stop_uics))
        except requests.RequestException as exc:
            print(f"(station name lookup failed: {exc} -- will show raw UIC codes instead)\n")

    for code in target_codes:
        if code not in found:
            continue
        trip_id, parsed, nxt, is_future = found[code]
        print("=" * 78)
        print(f"code={code}  train_number={parsed['train_number']}  "
              f"route={parsed['origin_uic']} -> {parsed['destination_uic']}")
        print(f"trip_id: {trip_id}")
        if nxt:
            stop_id = nxt.get("stop_id", "")
            m = parse.re.search(r"(\d{5,8})\Z", stop_id)
            uic = m.group(1) if m else None
            name = station_names.get(uic, {}).get("nom") if uic else None
            arrival = nxt.get("arrival", {}).get("time")
            departure = nxt.get("departure", {}).get("time")
            when = arrival or departure
            when_str = datetime.fromtimestamp(int(when)).strftime("%Y-%m-%d %H:%M:%S (local)") if when else "?"
            label = "next stop" if is_future else "last known stop (trip appears finished -- re-run for a fresher example)"
            print(f"{label}: {name or stop_id}  (UIC {uic})  at {when_str}")
        else:
            print("next stop: (no stop_time_update entries at all -- trip may be finishing)")
        print()

    if len(found) < len(target_codes):
        print("Tip: re-run in a few minutes -- not every code has an active train every moment "
              "(CTE/CRE/ICN/TRN/NA are all low-volume codes, some may run only at certain times of day).")


if __name__ == "__main__":
    sys.exit(main())
