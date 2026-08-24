#!/usr/bin/env python3
"""
Inspects tchoutchou_snapshot.db (5.28GB, last written 2026-08-18) to confirm what it
actually is before anyone deletes it. Two live hypotheses:

  1. It's a full raw-copy backup taken right before the 2026-08-18 platform-layer
     migration (HANDOFF.md's DROP TABLE platform_journeys/platform_calls surgery) -- a
     one-time safety copy that's just never been cleaned up since. Its size (5.28GB) is
     suspiciously close to HANDOFF.md's own "the tchoutchou.db that grew to 5.7GB"
     figure for the pre-migration database, and its date matches exactly.
  2. It's something else entirely (a deliberate ongoing backup, output from a different
     tool, etc.) that shouldn't be touched without a plan.

Note this is NOT the same file as tchoutchou_monitor.db (45.9MB) -- that one IS
monitor_snapshot.py's actual default output. tchoutchou_snapshot.db's name is close but
isn't monitor_snapshot.py's default --out, so it wasn't produced by an ordinary
monitor_snapshot.py run.

Usage:
    python check_snapshot.py --db tchoutchou_snapshot.db
"""
import argparse
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tchoutchou_snapshot.db")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()]
    print(f"=== {args.db} ===")
    print(f"{len(tables)} table(s): {tables}\n")

    for t in ["snapshots", "trip_updates", "stop_time_updates", "aggregation_state",
              "train_station_stats", "platform_journeys", "platform_calls"]:
        if t not in tables:
            print(f"{t:24s}  -- table not present")
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:24s}  {n} row(s)")

    # Old (pre-2026-08-18) vs new platform schema check -- the new schema has
    # train_number/calendar_date columns and no auto-increment `id`; the old one has
    # `id`/`platform_journey_id` and no train_number/calendar_date.
    if "platform_journeys" in tables:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(platform_journeys)").fetchall()]
        schema_kind = "OLD (pre-2026-08-18 append-only)" if "id" in cols and "train_number" not in cols else \
                      "NEW (UPSERT, post-2026-08-18)" if "train_number" in cols else "UNRECOGNIZED"
        print(f"\nplatform_journeys columns: {cols}")
        print(f"-> looks like the {schema_kind} schema")

    # Time range covered, if snapshots has any rows
    if "snapshots" in tables:
        row = conn.execute("SELECT MIN(fetched_at_utc), MAX(fetched_at_utc) FROM snapshots").fetchone()
        print(f"\nsnapshots time range: {row[0]}  to  {row[1]}")

    # Does it have the CURRENT schema's newer tables at all? (trip_finals, journal_size_limit-era
    # tables wouldn't exist in an old pre-migration copy)
    print(f"\nHas trip_finals table (added 2026-08-23, would only exist in a very recent copy): "
          f"{'trip_finals' in tables}")

    conn.close()


if __name__ == "__main__":
    main()
