#!/usr/bin/env python3
"""
Development-time validation of parse.py's trip_id extraction against SNCF's static
GTFS export (ground truth) -- NOT needed at runtime, and not called by ingest.py.

Three checks:
1. Train number: for every trip in trips.txt, does parse_trip_id()'s train_number
   match that trip's trip_headsign (SNCF's own field for commercial train number)?
2. Origin/destination UIC: for a random sample of trips, does parse_trip_id()'s
   origin_uic/destination_uic match the actual first/last stop (by stop_sequence) in
   stop_times.txt?
3. Service code discovery: does every trip yield a service_code (never None)? And a
   breakdown per code -- volume, sample route_short_name/route_type from routes.txt,
   sample train numbers -- to sanity-check parse.py's SERVICE_CODE_INFO mapping (or
   spot a new code SNCF has started using since that mapping was last reviewed).

Run this whenever you download a newer static GTFS export, to confirm SNCF hasn't
changed the trip_id convention parse.py relies on.

Usage:
    python validate_extraction.py --gtfs-dir Export_OpenData_SNCF_GTFS_NewTripId
    python validate_extraction.py --gtfs-dir <path> --route-sample-size 1000
"""

import argparse
import csv
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from parse import SERVICE_CODE_INFO, parse_trip_id

_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")


def validate_train_numbers(gtfs_dir: Path) -> bool:
    trips_path = gtfs_dir / "trips.txt"
    print(f"--- Train number vs. trip_headsign ({trips_path}) ---")

    total = 0
    matched = 0
    no_extraction = 0
    mismatches = []
    prefixes = set()

    with open(trips_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            trip_id = row["trip_id"]
            headsign = row["trip_headsign"]
            extracted = parse_trip_id(trip_id)["train_number"]

            m = re.match(r"\A([A-Za-z]+)", trip_id)
            if m:
                prefixes.add(m.group(1))

            if extracted is None:
                no_extraction += 1
                continue
            if extracted == headsign:
                matched += 1
            elif len(mismatches) < 10:
                mismatches.append((trip_id, headsign, extracted))

    print(f"total trips: {total}")
    print(f"train_number == trip_headsign: {matched}")
    print(f"train_number extraction returned None: {no_extraction}")
    print(f"mismatches: {len(mismatches)}{'+' if len(mismatches) == 10 else ''}")
    print(f"operator prefixes seen: {sorted(prefixes)}")
    if mismatches:
        print("sample mismatches (trip_id, trip_headsign, extracted):")
        for x in mismatches:
            print(" ", x)

    ok = matched == total
    print("PASS" if ok else "FAIL -- extraction logic in parse.py needs revisiting")
    print()
    return ok


def validate_routes(gtfs_dir: Path, sample_size: int) -> bool:
    trips_path = gtfs_dir / "trips.txt"
    stop_times_path = gtfs_dir / "stop_times.txt"
    print(f"--- Origin/destination UIC vs. first/last stop ({stop_times_path}) ---")

    with open(trips_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    sample = random.sample(rows, min(sample_size, len(rows)))
    sample_ids = {r["trip_id"] for r in sample}

    first_last = {}
    with open(stop_times_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = row["trip_id"]
            if tid not in sample_ids:
                continue
            seq = int(row["stop_sequence"])
            stop_id = row["stop_id"]
            rec = first_last.get(tid)
            if rec is None:
                first_last[tid] = [seq, stop_id, seq, stop_id]
            else:
                if seq < rec[0]:
                    rec[0], rec[1] = seq, stop_id
                if seq > rec[2]:
                    rec[2], rec[3] = seq, stop_id

    total = 0
    ok_origin = 0
    ok_dest = 0
    mismatches = []
    for tid, rec in first_last.items():
        parsed = parse_trip_id(tid)
        if parsed["origin_uic"] is None:
            continue
        total += 1
        first_uic_m = _STOP_UIC_RE.search(rec[1])
        last_uic_m = _STOP_UIC_RE.search(rec[3])
        first_uic = first_uic_m.group(1) if first_uic_m else None
        last_uic = last_uic_m.group(1) if last_uic_m else None
        origin_ok = first_uic == parsed["origin_uic"]
        dest_ok = last_uic == parsed["destination_uic"]
        if origin_ok:
            ok_origin += 1
        if dest_ok:
            ok_dest += 1
        if not (origin_ok and dest_ok) and len(mismatches) < 10:
            mismatches.append((tid, parsed["origin_uic"], first_uic, parsed["destination_uic"], last_uic))

    print(f"sampled trips with stop_times coverage: {total}")
    print(f"origin_uic correct: {ok_origin}/{total}")
    print(f"destination_uic correct: {ok_dest}/{total}")
    if mismatches:
        print("sample mismatches (trip_id, parsed_origin, actual_first_uic, parsed_dest, actual_last_uic):")
        for x in mismatches:
            print(" ", x)

    ok = total > 0 and ok_origin == total and ok_dest == total
    print("PASS" if ok else "FAIL -- origin/destination extraction needs revisiting")
    print()
    return ok


def discover_service_codes(gtfs_dir: Path) -> bool:
    trips_path = gtfs_dir / "trips.txt"
    routes_path = gtfs_dir / "routes.txt"
    print(f"--- Service code discovery ({trips_path}) ---")

    routes = {}
    if routes_path.exists():
        with open(routes_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                routes[row["route_id"]] = row

    total = 0
    no_code = 0
    code_counter = Counter()
    code_to_routes = defaultdict(set)
    unmapped_codes = set()

    with open(trips_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            total += 1
            code = parse_trip_id(row["trip_id"])["service_code"]
            if code is None:
                no_code += 1
                continue
            code_counter[code] += 1
            if code not in SERVICE_CODE_INFO:
                unmapped_codes.add(code)
            route = routes.get(row["route_id"])
            if route and len(code_to_routes[code]) < 5:
                code_to_routes[code].add((route.get("route_short_name"), route.get("route_type")))

    print(f"total trips: {total}")
    print(f"trips with no service_code extracted: {no_code}")
    print()
    print(f"{'code':<6} {'count':>7}  {'label (confidence)':<30} sample (route_short_name, route_type)")
    for code, count in code_counter.most_common():
        label, confidence, _ = SERVICE_CODE_INFO.get(code, (None, "UNKNOWN", None))
        label_str = f"{label or '?'} ({confidence})"
        print(f"{code:<6} {count:>7}  {label_str:<30} {sorted(code_to_routes[code])[:4]}")

    if unmapped_codes:
        print()
        print(f"NEW codes not in parse.py's SERVICE_CODE_INFO: {sorted(unmapped_codes)}")
        print("-> investigate and add them (with evidence) before trusting train_type for these.")

    ok = no_code == 0
    print("PASS" if ok else "FAIL -- some trip_ids have no second colon-segment")
    print()
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs-dir", default="Export_OpenData_SNCF_GTFS_NewTripId",
                     help="Path to an extracted static GTFS export (must contain trips.txt and stop_times.txt)")
    ap.add_argument("--route-sample-size", type=int, default=500)
    args = ap.parse_args()

    gtfs_dir = Path(args.gtfs_dir)
    if not (gtfs_dir / "trips.txt").exists():
        print(f"trips.txt not found in {gtfs_dir} -- download and extract the static GTFS export first.")
        sys.exit(1)

    ok1 = validate_train_numbers(gtfs_dir)
    ok2 = validate_routes(gtfs_dir, args.route_sample_size)
    ok3 = discover_service_codes(gtfs_dir)

    sys.exit(0 if (ok1 and ok2 and ok3) else 1)


if __name__ == "__main__":
    main()
