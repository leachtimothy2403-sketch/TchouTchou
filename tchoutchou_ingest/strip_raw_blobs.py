#!/usr/bin/env python3
"""
Drops the snapshots.raw_gzip safety-net blobs for polls older than --keep-hours,
while keeping every parsed row (trip_updates, stop_time_updates, service_alerts,
platform_*) and the snapshot row itself (metadata only).

Why (2026-10-01, disk full at db=21GB): every poll of every feed stores its full
gzip'd response in snapshots.raw_gzip, every 120s, for the whole 5-day raw window.
ingest.py's own comment calls this "the single biggest lever left on db size";
it was kept on purpose until parsing proved stable. This script is the middle
ground: keep the blobs for the last day or so (enough to re-diagnose a fresh
parsing bug), drop them after that.

Disk effect: SQLite does NOT shrink the file when blobs are cleared -- the pages go
to the freelist and get reused by new inserts. So the .db stops GROWING (for a while)
immediately, but only shrinks on disk after a manual VACUUM (needs free disk >= db
size). Works in small committed batches with TRUNCATE checkpoints so the -wal file
stays small even on a nearly-full disk. Safe to run while ingest.py is running.

Usage:
    python strip_raw_blobs.py --db tchoutchou.db --dry-run          # just measure
    python strip_raw_blobs.py --db tchoutchou.db --keep-hours 24
"""

import argparse
import sqlite3
import time
from datetime import datetime, timedelta, timezone


def gb(n):
    return f"{(n or 0) / 1e9:.2f} GB"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--keep-hours", type=float, default=24)
    ap.add_argument("--batch", type=int, default=200, help="snapshots cleared per commit")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    cur = conn.cursor()

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.keep_hours)).isoformat()
    page_size = cur.execute("PRAGMA page_size").fetchone()[0]
    page_count = cur.execute("PRAGMA page_count").fetchone()[0]
    free_before = cur.execute("PRAGMA freelist_count").fetchone()[0]

    print(f"db: {gb(page_count * page_size)} total, {gb(free_before * page_size)} already free inside the file")
    print(f"keeping blobs newer than {cutoff}\n")
    print(f"{'feed':<18}{'blobs':>9}{'blob size':>12}{'eligible':>12}")
    total = eligible_total = 0
    for feed, n, size, elig in cur.execute(
        "SELECT feed_name, COUNT(raw_gzip), SUM(length(raw_gzip)), "
        "       SUM(CASE WHEN fetched_at_utc < ? THEN length(raw_gzip) END) "
        "FROM snapshots GROUP BY feed_name", (cutoff,)
    ).fetchall():
        total += size or 0
        eligible_total += elig or 0
        print(f"{feed:<18}{n:>9}{gb(size):>12}{gb(elig):>12}")
    print(f"{'TOTAL':<18}{'':>9}{gb(total):>12}{gb(eligible_total):>12}")

    if args.dry_run:
        print("\nDry run -- nothing changed.")
        return

    ids = [r[0] for r in cur.execute(
        "SELECT id FROM snapshots WHERE raw_gzip IS NOT NULL AND fetched_at_utc < ? ORDER BY id",
        (cutoff,),
    ).fetchall()]
    print(f"\nClearing {len(ids)} blobs in batches of {args.batch}...")
    t0 = time.time()
    for i in range(0, len(ids), args.batch):
        chunk = ids[i:i + args.batch]
        cur.execute(f"UPDATE snapshots SET raw_gzip = NULL WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
        if (i // args.batch) % 10 == 0:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            print(f"  {min(i + args.batch, len(ids))}/{len(ids)}  ({time.time() - t0:.0f}s)")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    free_after = cur.execute("PRAGMA freelist_count").fetchone()[0]
    print(f"\nDone. Free space inside the db: {gb(free_before * page_size)} -> {gb(free_after * page_size)}")
    print("New data will reuse that space, so the .db file stops growing until it's used up.")
    print("To actually shrink the file: VACUUM by hand once free disk >= db size.")
    conn.close()


if __name__ == "__main__":
    main()
