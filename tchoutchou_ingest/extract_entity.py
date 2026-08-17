#!/usr/bin/env python3
"""
Reconstructs full entity JSON on demand from a stored snapshot's raw_gzip blob --
the thing that used to be pre-computed and stored per-row as `raw_json` before that
turned out to be ~45% of the database for zero extra information (raw_gzip already
has 100% fidelity, compressed). This does the same MessageToDict parse parse.py does
during ingestion, just lazily, only when you actually need to look at one entity.

Usage:
    # every entity in a given snapshot
    python extract_entity.py --db tchoutchou.db --snapshot-id 1234

    # a specific trip across every snapshot that captured it (its full history)
    python extract_entity.py --db tchoutchou.db --trip-id "OCESN9898F1187_F:OUI:..."

    # just the latest snapshot of a feed
    python extract_entity.py --db tchoutchou.db --feed trip_updates --latest
"""

import argparse
import gzip
import json
import sqlite3
import sys

from google.transit import gtfs_realtime_pb2

import parse


def iter_entities(raw_gzip: bytes):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(gzip.decompress(raw_gzip))
    for entity in feed.entity:
        yield entity


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--snapshot-id", type=int, help="Dump every entity in this snapshot")
    ap.add_argument("--trip-id", help="Dump this trip_id's entity from every snapshot that has it")
    ap.add_argument("--feed", default="trip_updates", choices=["trip_updates", "service_alerts"])
    ap.add_argument("--latest", action="store_true", help="With --feed: just the most recent snapshot")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    if args.snapshot_id is not None:
        snapshot_ids = [args.snapshot_id]
    elif args.latest:
        cur.execute(
            "SELECT id FROM snapshots WHERE feed_name=? AND raw_gzip IS NOT NULL "
            "ORDER BY fetched_at_utc DESC LIMIT 1",
            (args.feed,),
        )
        row = cur.fetchone()
        if not row:
            print("No snapshot with a stored raw_gzip found for that feed.", file=sys.stderr)
            sys.exit(1)
        snapshot_ids = [row[0]]
    elif args.trip_id:
        cur.execute(
            "SELECT DISTINCT snapshot_id FROM trip_updates WHERE trip_id=?", (args.trip_id,)
        )
        snapshot_ids = [r[0] for r in cur.fetchall()]
        if not snapshot_ids:
            print(f"trip_id {args.trip_id!r} not found in trip_updates.", file=sys.stderr)
            sys.exit(1)
    else:
        ap.error("Specify one of --snapshot-id, --trip-id, or --feed with --latest")

    for snap_id in snapshot_ids:
        cur.execute("SELECT raw_gzip, fetched_at_utc, feed_name FROM snapshots WHERE id=?", (snap_id,))
        row = cur.fetchone()
        if row is None or row[0] is None:
            print(f"snapshot {snap_id}: no raw_gzip stored (was the collector run with --no-raw?)",
                  file=sys.stderr)
            continue
        raw_gzip, fetched_at, feed_name = row

        for entity in iter_entities(raw_gzip):
            if args.trip_id:
                tid = entity.trip_update.trip.trip_id if entity.HasField("trip_update") else None
                if tid != args.trip_id:
                    continue
            ent = parse.entity_to_dict(entity)
            print(f"--- snapshot {snap_id} ({feed_name}, fetched {fetched_at}) ---")
            print(json.dumps(ent, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
