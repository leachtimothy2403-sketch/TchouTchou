#!/usr/bin/env python3
"""One-off diagnostic: how much of the db is raw_gzip blobs vs everything else.
Doesn't need dbstat -- LENGTH() on a BLOB column works on any stock SQLite build.
Usage: python check_raw_blob_share.py --db tchoutchou.db
"""
import argparse
import os
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="tchoutchou.db")
args = ap.parse_args()

conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
cur = conn.cursor()

total_blob = cur.execute(
    "SELECT SUM(LENGTH(raw_gzip)) FROM snapshots WHERE raw_gzip IS NOT NULL"
).fetchone()[0] or 0

db_size = os.path.getsize(args.db)

print(f"DB file size:        {db_size:,} bytes ({db_size/1024/1024/1024:.2f} GB)")
print(f"raw_gzip blob total: {total_blob:,} bytes ({total_blob/1024/1024:.1f} MB)")
print(f"raw_gzip share:      {total_blob/db_size*100:.1f}% of the file")
print()
print("By feed:")
for feed, sz, n, avg in cur.execute(
    "SELECT feed_name, SUM(LENGTH(raw_gzip)), COUNT(*), AVG(LENGTH(raw_gzip)) "
    "FROM snapshots WHERE raw_gzip IS NOT NULL GROUP BY feed_name ORDER BY 2 DESC"
).fetchall():
    print(f"  {feed:16s} {sz:>14,} bytes  ({sz/1024/1024:>8.1f} MB)  "
          f"{n:>6,} polls  avg {avg/1024:>7.1f} KB/poll")

conn.close()
