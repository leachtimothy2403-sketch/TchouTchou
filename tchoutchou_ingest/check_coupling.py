#!/usr/bin/env python3
"""
Tests the coupled-unit theory for the duplicate-stop-in-one-poll bug found via
check_departure.py: is the duplicate stop_time_update row coming from the SAME
trip_update entity (a within-entity list duplicate -- a parsing/feed-list glitch), or
from TWO DIFFERENT trip_update entities that happen to share the same trip_id within one
poll (which would mean two physically distinct vehicles -- e.g. a coupled unit -- are
both being folded into what aggregate.py treats as a single trip)?

Distinguishing signal: trip_updates.entity_id (each GTFS-RT FeedEntity has its own "id"
field) and vehicle_id/vehicle_label. If a single snapshot has TWO trip_updates rows for
the same (trip_id, start_date) with DIFFERENT entity_id/vehicle_id, that's two distinct
physical entities under one trip_id -- consistent with coupling. If it's ONE
trip_updates row whose OWN stop_time_update list just contains the stop twice, that's a
within-entity duplicate instead -- a different kind of bug, unrelated to coupling.

Usage:
    python check_coupling.py --db tchoutchou.db --train 117137 --station 87113001
"""
import argparse
import re
import sqlite3

_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")


def extract_uic(stop_id):
    m = _STOP_UIC_RE.search(stop_id or "")
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--train", required=True)
    ap.add_argument("--station", required=True)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    trips = conn.execute(
        "SELECT trip_id, start_date FROM aggregation_state WHERE train_number=? AND cancelled=0",
        (args.train,),
    ).fetchall()

    for trip_id, start_date in trips:
        # For every snapshot that reported this (trip_id, start_date), how many DISTINCT
        # trip_update rows (i.e. distinct entities) exist, and how many stop_time_update
        # rows at the target station does each carry?
        rows = conn.execute(
            "SELECT s.fetched_at_utc, s.id AS snap_id, tu.id AS tu_id, tu.entity_id, "
            "tu.vehicle_id, tu.vehicle_label, stu.stop_id, stu.arrival_delay, stu.departure_delay "
            "FROM trip_updates tu "
            "JOIN snapshots s ON s.id = tu.snapshot_id "
            "LEFT JOIN stop_time_updates stu ON stu.trip_update_id = tu.id "
            "WHERE tu.trip_id=? AND tu.start_date=? "
            "ORDER BY s.fetched_at_utc ASC, tu.id ASC",
            (trip_id, start_date),
        ).fetchall()
        if not rows:
            continue

        # Group by (fetched_at_utc, snap_id) to see, per poll, how many DISTINCT
        # trip_update entities existed for this trip_id, and what each contributed at
        # the target station.
        by_poll = {}
        for fetched, snap_id, tu_id, entity_id, vehicle_id, vehicle_label, stop_id, arr, dep in rows:
            key = (fetched, snap_id)
            by_poll.setdefault(key, {})
            by_poll[key].setdefault(tu_id, {
                "entity_id": entity_id, "vehicle_id": vehicle_id, "vehicle_label": vehicle_label,
                "stops_at_station": [],
            })
            if stop_id and extract_uic(stop_id) == args.station:
                by_poll[key][tu_id]["stops_at_station"].append((stop_id, arr, dep))

        multi_entity_polls = {k: v for k, v in by_poll.items() if len(v) > 1}
        polls_with_station_dupe = {
            k: v for k, v in by_poll.items()
            if sum(len(e["stops_at_station"]) for e in v.values()) > 1
        }

        if not multi_entity_polls and not polls_with_station_dupe:
            continue

        print(f"=== trip_id={trip_id} start_date={start_date} ===")
        print(f"  {len(by_poll)} poll(s) total, {len(multi_entity_polls)} poll(s) with >1 distinct "
              f"trip_update ENTITY for this trip_id, {len(polls_with_station_dupe)} poll(s) with "
              f"the target station appearing more than once")

        # Show full detail for a few of the interesting polls
        interesting = list(polls_with_station_dupe.keys())[:3] or list(multi_entity_polls.keys())[:3]
        for fetched, snap_id in interesting:
            print(f"\n  -- poll fetched_at_utc={fetched} snap_id={snap_id} --")
            for tu_id, info in by_poll[(fetched, snap_id)].items():
                print(f"    trip_update_id={tu_id} entity_id={info['entity_id']} "
                      f"vehicle_id={info['vehicle_id']} vehicle_label={info['vehicle_label']}")
                for stop_id, arr, dep in info["stops_at_station"]:
                    print(f"      stop_id={stop_id!r} arrival_delay={arr} departure_delay={dep}")
        print()

    conn.close()


if __name__ == "__main__":
    main()
