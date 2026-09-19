#!/usr/bin/env python3
"""
Age-based purge for the platform layer (platform_journeys, platform_calls).

These two tables were redesigned (2026-08-18) to UPSERT to final state -- one row per
(train_number, calendar_date) / per stop, never per-poll history -- so purge_raw.py's
docstring correctly says there's "nothing left to purge" in the append-only sense. But
because calendar_date is part of the primary key, a full day's worth of rows is added
every day forever with no expiry, which is why they've grown to 418k+ / 4.7M+ rows
(2026-09-13 snapshot) and are the main reason tchoutchou.db keeps growing after the
2026-08-23 WAL fix.

Same safety pattern as purge_raw.py: only deletes (train_number, calendar_date) rows
that are already folded into platform_aggregation_state (i.e. aggregate.py's
process_platform_trip() has already folded them into platform_variants /
platform_lead_time_stats). Never deletes a day that hasn't been aggregated yet.

Usage:
    python purge_platform.py --db tchoutchou.db --dry-run
    python purge_platform.py --db tchoutchou.db --retention-days 14
    python purge_platform.py --db tchoutchou.db --retention-days 14 --vacuum
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--retention-days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vacuum", action="store_true",
                     help="Reclaim disk space after deleting (needs ~2x db size free -- "
                          "do NOT use this on a near-full disk, see README/HANDOFF)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=args.retention_days)).strftime("%Y%m%d")
    print(f"Retention: {args.retention_days} days -- cutoff_date={cutoff_date}\n")

    # --- safety check: anything old but NOT yet aggregated? ---
    cur.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT pj.train_number, pj.calendar_date FROM platform_journeys pj "
        "  LEFT JOIN platform_aggregation_state pas "
        "    ON pas.train_number = pj.train_number AND pas.calendar_date = pj.calendar_date "
        "  WHERE pj.calendar_date < ? AND pas.train_number IS NULL"
        ")",
        (cutoff_date,),
    )
    unaggregated_old = cur.fetchone()[0]
    if unaggregated_old:
        print(f"WARNING: {unaggregated_old} (train,date) journey(s) older than the retention "
              f"window have NOT been aggregated yet. They will be SKIPPED (not deleted) this run.")
        print("         Run aggregate.py first if you want them folded in before purging.\n")

    # --- journeys eligible: aggregated + past cutoff ---
    cur.execute(
        "SELECT pj.train_number, pj.calendar_date FROM platform_journeys pj "
        "JOIN platform_aggregation_state pas "
        "  ON pas.train_number = pj.train_number AND pas.calendar_date = pj.calendar_date "
        "WHERE pj.calendar_date < ?",
        (cutoff_date,),
    )
    keys = cur.fetchall()

    pc_count = 0
    if keys:
        for chunk in chunks(keys, 500):
            placeholders = ",".join("(?,?)" for _ in chunk)
            flat = [v for pair in chunk for v in pair]
            cur.execute(
                f"SELECT COUNT(*) FROM platform_calls WHERE (train_number, calendar_date) IN ({placeholders})",
                flat,
            )
            pc_count += cur.fetchone()[0]

    print(f"platform_journeys rows eligible for deletion: {len(keys)}")
    print(f"platform_calls rows eligible for deletion:     {pc_count}")

    if args.dry_run:
        print("\nDry run -- nothing deleted.")
        return

    pc_deleted = 0
    pj_deleted = 0
    for chunk in chunks(keys, 500):
        placeholders = ",".join("(?,?)" for _ in chunk)
        flat = [v for pair in chunk for v in pair]
        cur.execute(f"DELETE FROM platform_calls WHERE (train_number, calendar_date) IN ({placeholders})", flat)
        pc_deleted += cur.rowcount
        cur.execute(f"DELETE FROM platform_journeys WHERE (train_number, calendar_date) IN ({placeholders})", flat)
        pj_deleted += cur.rowcount
    conn.commit()

    print(f"\nDeleted: {pj_deleted} platform_journeys, {pc_deleted} platform_calls.")
    print("Note: rows are gone, but SQLite won't shrink the file on disk until VACUUM runs --")
    print("freed pages are reused for future writes instead, which is what you want on a near-full disk.")

    if args.vacuum:
        print("Running VACUUM (this needs roughly as much free disk as the db's current size)...")
        conn.execute("VACUUM")
        print("VACUUM done.")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
