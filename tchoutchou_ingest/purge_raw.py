#!/usr/bin/env python3
"""
Deletes raw-layer rows older than --retention-days -- but ONLY for (trip_id, start_date)
trips that aggregate.py has already folded into the permanent stats tables (tracked in
aggregation_state). Refuses to delete raw data for a trip that isn't in there yet, so
you can never lose data that hasn't been turned into a permanent statistic -- run
aggregate.py first, then this.

service_alerts aren't part of the aggregation pipeline (see README), so they're purged
by age alone, independent of aggregation_state.

Also purges the platform layer (platform_journeys, platform_calls) using the same
"only if already aggregated" rule, gated on platform_aggregation_state instead of
aggregation_state (SIRI's identity key is train_number+calendar_date, not
trip_id+start_date -- see db.py and aggregate.py).

Usage:
    python purge_raw.py --db tchoutchou.db --dry-run           # preview only, deletes nothing
    python purge_raw.py --db tchoutchou.db --retention-days 60
    python purge_raw.py --db tchoutchou.db --vacuum            # also reclaim disk space (slower)
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
    ap.add_argument("--retention-days", type=int, default=90)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vacuum", action="store_true",
                     help="Reclaim disk space after deleting (can take a while and briefly needs ~2x disk space)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=args.retention_days)).strftime("%Y%m%d")
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=args.retention_days)).isoformat()
    print(f"Retention: {args.retention_days} days -- cutoff_date={cutoff_date}, cutoff_ts={cutoff_ts}\n")

    # --- safety check: anything old but NOT yet aggregated? ---
    cur.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT tu.trip_id, tu.start_date FROM trip_updates tu "
        "  LEFT JOIN aggregation_state ags ON ags.trip_id=tu.trip_id AND ags.start_date=tu.start_date "
        "  WHERE tu.start_date < ? AND ags.trip_id IS NULL"
        ")",
        (cutoff_date,),
    )
    unaggregated_old = cur.fetchone()[0]
    if unaggregated_old:
        print(f"WARNING: {unaggregated_old} trip(s) older than the retention window have NOT been "
              f"aggregated yet. Their raw data will be SKIPPED (not deleted) this run.")
        print("         Run aggregate.py first if you want them folded into permanent stats before purging.\n")

    # --- trip_updates / stop_time_updates eligible for deletion: aggregated + past cutoff ---
    cur.execute(
        "SELECT tu.id FROM trip_updates tu "
        "JOIN aggregation_state ags ON ags.trip_id=tu.trip_id AND ags.start_date=tu.start_date "
        "WHERE tu.start_date < ?",
        (cutoff_date,),
    )
    tu_ids = [r[0] for r in cur.fetchall()]

    stu_count = 0
    if tu_ids:
        for chunk in chunks(tu_ids, 500):
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(f"SELECT COUNT(*) FROM stop_time_updates WHERE trip_update_id IN ({placeholders})", chunk)
            stu_count += cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM service_alerts sa JOIN snapshots s ON s.id = sa.snapshot_id "
        "WHERE s.fetched_at_utc < ?",
        (cutoff_ts,),
    )
    sa_count = cur.fetchone()[0]

    print(f"trip_updates rows eligible for deletion:      {len(tu_ids)}")
    print(f"stop_time_updates rows eligible for deletion:  {stu_count}")
    print(f"service_alerts rows eligible for deletion:     {sa_count}")

    # --- platform_journeys / platform_calls: aggregated + past cutoff (by calendar_date) ---
    cur.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT DISTINCT pj.train_number, pj.calendar_date FROM platform_journeys pj "
        "  LEFT JOIN platform_aggregation_state pas "
        "    ON pas.train_number=pj.train_number AND pas.calendar_date=pj.calendar_date "
        "  WHERE pj.calendar_date < ? AND pas.train_number IS NULL"
        ")",
        (cutoff_date,),
    )
    unaggregated_old_platform = cur.fetchone()[0]
    if unaggregated_old_platform:
        print(f"WARNING: {unaggregated_old_platform} platform journey(s) older than the retention window "
              f"have NOT been aggregated yet. Their raw data will be SKIPPED (not deleted) this run.\n")

    cur.execute(
        "SELECT pj.id FROM platform_journeys pj "
        "JOIN platform_aggregation_state pas "
        "  ON pas.train_number=pj.train_number AND pas.calendar_date=pj.calendar_date "
        "WHERE pj.calendar_date < ?",
        (cutoff_date,),
    )
    pj_ids = [r[0] for r in cur.fetchall()]

    pc_count = 0
    if pj_ids:
        for chunk in chunks(pj_ids, 500):
            placeholders = ",".join("?" for _ in chunk)
            cur.execute(f"SELECT COUNT(*) FROM platform_calls WHERE platform_journey_id IN ({placeholders})", chunk)
            pc_count += cur.fetchone()[0]

    print(f"platform_journeys rows eligible for deletion:  {len(pj_ids)}")
    print(f"platform_calls rows eligible for deletion:     {pc_count}")

    if args.dry_run:
        print("\nDry run -- nothing deleted.")
        return

    stu_deleted = 0
    tu_deleted = 0
    for chunk in chunks(tu_ids, 500):
        placeholders = ",".join("?" for _ in chunk)
        cur.execute(f"DELETE FROM stop_time_updates WHERE trip_update_id IN ({placeholders})", chunk)
        stu_deleted += cur.rowcount
        cur.execute(f"DELETE FROM trip_updates WHERE id IN ({placeholders})", chunk)
        tu_deleted += cur.rowcount
    conn.commit()

    cur.execute(
        "DELETE FROM service_alerts WHERE snapshot_id IN "
        "(SELECT id FROM snapshots WHERE fetched_at_utc < ?)",
        (cutoff_ts,),
    )
    sa_deleted = cur.rowcount
    conn.commit()

    pc_deleted = 0
    pj_deleted = 0
    for chunk in chunks(pj_ids, 500):
        placeholders = ",".join("?" for _ in chunk)
        cur.execute(f"DELETE FROM platform_calls WHERE platform_journey_id IN ({placeholders})", chunk)
        pc_deleted += cur.rowcount
        cur.execute(f"DELETE FROM platform_journeys WHERE id IN ({placeholders})", chunk)
        pj_deleted += cur.rowcount
    conn.commit()

    # --- snapshots: only ones with zero remaining trip_updates/service_alerts/platform_journeys referencing them ---
    cur.execute(
        "DELETE FROM snapshots WHERE fetched_at_utc < ? "
        "AND id NOT IN (SELECT DISTINCT snapshot_id FROM trip_updates) "
        "AND id NOT IN (SELECT DISTINCT snapshot_id FROM service_alerts) "
        "AND id NOT IN (SELECT DISTINCT snapshot_id FROM platform_journeys)",
        (cutoff_ts,),
    )
    snapshots_deleted = cur.rowcount
    conn.commit()

    print(f"\nDeleted: {tu_deleted} trip_updates, {stu_deleted} stop_time_updates, "
          f"{sa_deleted} service_alerts, {pj_deleted} platform_journeys, {pc_deleted} platform_calls, "
          f"{snapshots_deleted} snapshots.")

    if args.vacuum:
        print("Running VACUUM...")
        conn.execute("VACUUM")
        print("VACUUM done.")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
