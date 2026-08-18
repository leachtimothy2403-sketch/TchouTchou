#!/usr/bin/env python3
"""
Exports a small "monitoring snapshot" from a running collector db -- everything useful
for a remote health check (row counts, a recent sample per raw-layer table, the whole
permanent layer, ingestion_log, and snapshot metadata) without the raw_gzip blobs or the
millions of raw-layer rows that make the source db multi-gigabyte.

The output is meant to be copied off a VPS quickly for a check-in -- unlike the full db,
which at multi-GB scale is impractical to transfer. Not a full replica: the unbounded
raw-layer tables (trip_updates, stop_time_updates, service_alerts) are sampled (most
recent N rows), not copied in full. The permanent layer (trains, train_station_stats,
train_stats, station_stats, etc.) IS copied in full since it stays small even at scale
(see README's storage projections). platform_journeys/platform_calls are also copied in
full -- since the 2026-08-18 redesign they're UPSERTed to final state (one row per
train_number+calendar_date / per stop, not one row per poll), so they're bounded and
small like the permanent layer, not unbounded like the other raw tables.

Usage:
    python monitor_snapshot.py --db tchoutchou.db --out tchoutchou_monitor.db
    python monitor_snapshot.py --db tchoutchou.db --out tchoutchou_monitor.db --sample-rows 1000
"""

import argparse
import os
import sqlite3

# Tables that stay small even at scale (see README's ~100-150MB long-term projection
# for the permanent layer) -- copy in full.
SMALL_TABLES_FULL = [
    "ingestion_log",
    "stations",
    "aggregation_state",
    "platform_aggregation_state",
    "trains",
    "train_station_stats",
    "train_stats",
    "station_stats",
    "train_route_variants",
    "platform_variants",
    "platform_lead_time_stats",
    # platform_journeys/platform_calls: UPSERTed to final state since the 2026-08-18
    # redesign (one row per train_number+calendar_date / per stop, not per poll) --
    # bounded and small like the rest of this list, not unbounded like RAW_LAYER_TABLES.
    # Also: neither table has an `id` column anymore (composite natural PK instead), so
    # they can't use RAW_LAYER_TABLES' "ORDER BY id DESC LIMIT n" sampling logic anyway.
    "platform_journeys",
    "platform_calls",
]

# Raw-layer tables that grow unbounded with collection time -- only row count + a
# recent sample, never copied in full.
RAW_LAYER_TABLES = [
    "trip_updates",
    "stop_time_updates",
    "service_alerts",
]

DEFAULT_SAMPLE_ROWS = 500


def table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def create_table_like(src, out, table_name, column_filter=None):
    """Recreates table_name in `out` using the source's own CREATE TABLE statement,
    optionally dropping some columns (used for snapshots -> drop raw_gzip)."""
    if column_filter is None:
        create_sql = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()[0]
        out.execute(create_sql)
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table_name})").fetchall()]
        return cols
    else:
        cols = [r[1] for r in src.execute(f"PRAGMA table_info({table_name})").fetchall()]
        keep = [c for c in cols if c not in column_filter]
        out.execute(f"CREATE TABLE {table_name} ({', '.join(keep)})")
        return keep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db", help="Source (full) database")
    ap.add_argument("--out", default="tchoutchou_monitor.db", help="Output (small) snapshot database")
    ap.add_argument("--sample-rows", type=int, default=DEFAULT_SAMPLE_ROWS,
                     help=f"Most recent rows to keep per raw-layer table (default {DEFAULT_SAMPLE_ROWS})")
    args = ap.parse_args()

    if os.path.exists(args.out):
        os.remove(args.out)

    src = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    out = sqlite3.connect(args.out)

    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    # --- snapshots: everything except the raw_gzip blob column ---
    if "snapshots" in tables:
        cols = create_table_like(src, out, "snapshots", column_filter={"raw_gzip"})
        col_list = ", ".join(cols)
        rows = src.execute(f"SELECT {col_list} FROM snapshots").fetchall()
        if rows:
            out.executemany(f"INSERT INTO snapshots VALUES ({','.join('?' for _ in cols)})", rows)
        print(f"snapshots: copied {len(rows)} rows (raw_gzip column dropped)")

    # --- small tables: full copy ---
    for t in SMALL_TABLES_FULL:
        if t not in tables:
            continue
        cols = create_table_like(src, out, t)
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
        if rows:
            out.executemany(f"INSERT INTO {t} VALUES ({','.join('?' for _ in cols)})", rows)
        print(f"{t}: copied {len(rows)} rows (full)")

    # --- raw-layer tables: row count + most recent sample only ---
    out.execute("CREATE TABLE _raw_layer_counts (table_name TEXT PRIMARY KEY, row_count INTEGER, sampled_rows INTEGER)")
    for t in RAW_LAYER_TABLES:
        if t not in tables:
            continue
        total = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols = create_table_like(src, out, t)
        col_list = ", ".join(cols)
        rows = src.execute(f"SELECT {col_list} FROM {t} ORDER BY id DESC LIMIT ?", (args.sample_rows,)).fetchall()
        if rows:
            out.executemany(f"INSERT INTO {t} VALUES ({','.join('?' for _ in cols)})", rows)
        out.execute("INSERT INTO _raw_layer_counts VALUES (?, ?, ?)", (t, total, len(rows)))
        print(f"{t}: {total:,} total rows, sampled {len(rows)} most recent")

    # --- a per-table on-disk size breakdown from the SOURCE db, so size questions
    # ("why is it 5.5GB") are answerable without needing the full file at all ---
    out.execute("CREATE TABLE _source_table_sizes (table_name TEXT PRIMARY KEY, bytes INTEGER, pages INTEGER)")
    try:
        size_rows = src.execute(
            "SELECT name, SUM(pgsize), COUNT(*) FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC"
        ).fetchall()
        out.executemany("INSERT INTO _source_table_sizes VALUES (?, ?, ?)", size_rows)
        print(f"\nsource table size breakdown: {len(size_rows)} entries (dbstat)")
    except sqlite3.OperationalError as exc:
        print(f"\ncouldn't read dbstat (not compiled into this SQLite build): {exc}")

    out.execute(
        "CREATE TABLE _source_file_info (key TEXT PRIMARY KEY, value TEXT)"
    )
    out.execute("INSERT INTO _source_file_info VALUES ('source_db_path', ?)", (os.path.abspath(args.db),))
    out.execute("INSERT INTO _source_file_info VALUES ('source_db_bytes', ?)", (str(os.path.getsize(args.db)),))
    for wal_suffix in ("-wal", "-shm"):
        p = args.db + wal_suffix
        if os.path.exists(p):
            out.execute("INSERT INTO _source_file_info VALUES (?, ?)", (f"source_db{wal_suffix}_bytes", str(os.path.getsize(p))))

    out.commit()
    src.close()
    out.close()

    print(f"\nWrote {args.out} ({os.path.getsize(args.out) / 1024 / 1024:.2f} MB) "
          f"from source {os.path.getsize(args.db) / 1024 / 1024 / 1024:.2f} GB")


if __name__ == "__main__":
    main()
