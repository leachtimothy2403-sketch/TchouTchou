#!/usr/bin/env python3
"""
One-shot feed inspector. Run this BEFORE the long collection run.

Fetches trip_updates once and pretty-prints a handful of raw entities so you can:
  - confirm the real trip_id format and manually verify (or fix) the commercial
    train number heuristic in parse.py before trusting that column,
  - sanity-check that fields we assumed exist (vehicle, delay, uncertainty) actually
    show up in practice.

Usage:
    python peek.py                  # 5 sample trip_update entities
    python peek.py --count 15
    python peek.py --feed service_alerts
"""

import argparse
import json

import requests
from google.transit import gtfs_realtime_pb2

import parse
from ingest import FEEDS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feed", default="trip_updates", choices=list(FEEDS.keys()))
    ap.add_argument("--count", type=int, default=5)
    args = ap.parse_args()

    url = FEEDS[args.feed]
    print(f"Fetching {url} ...")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    print(f"gtfs_realtime_version={feed.header.gtfs_realtime_version} "
          f"header_timestamp={feed.header.timestamp} entities={len(feed.entity)}\n")

    for i, entity in enumerate(feed.entity[:args.count]):
        ent = parse.entity_to_dict(entity)
        print("=" * 80)
        print(json.dumps(ent, indent=2, ensure_ascii=False))

        if entity.HasField("trip_update"):
            trip_id = ent.get("trip_update", {}).get("trip", {}).get("trip_id")
            guess = parse.guess_commercial_train_number(trip_id)
            print(f"\n>>> trip_id={trip_id!r}  guessed_commercial_train_number={guess!r}")
            print(">>> Does that guess look right? If not, fix guess_commercial_train_number() in parse.py.")

    if len(feed.entity) > args.count:
        print(f"\n... ({len(feed.entity) - args.count} more entities not shown)")


if __name__ == "__main__":
    main()
