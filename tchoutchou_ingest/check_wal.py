#!/usr/bin/env python3
"""
Diagnoses and (where safe) fixes a runaway SQLite WAL file.

Run this directly on the VPS, in the same folder as tchoutchou.db (or pass --db).
Safe to run while ingest.py / the API are live -- checkpoint doesn't require
stopping the writer, it just needs a moment where no other connection is mid-read.

What it does, in order:
  1. Reports current .db / .db-wal / .db-shm sizes and PRAGMA wal_autocheckpoint.
  2. Runs PRAGMA wal_checkpoint(PASSIVE) -- never blocks anything, does whatever
     it safely can right now.
  3. Retries PRAGMA wal_checkpoint(TRUNCATE) a few times (this is the one that
     actually shrinks the file on disk) -- if it keeps reporting busy=1 across
     all retries, something is holding a read/write connection open for a
     sustained period, not just for the length of one query.
  4. Reports sizes again so you can see exactly what was reclaimed.

Usage:
    python check_wal.py --db tchoutchou.db
    python check_wal.py --db C:\\TchouTchou\\tchoutchou_ingest\\tchoutchou.db
"""
import argparse
import os
import sqlite3
import time


def sizes(db_path):
    out = {}
    for suffix, label in (("", "db"), ("-wal", "wal"), ("-shm", "shm")):
        p = db_path + suffix
        out[label] = os.path.getsize(p) if os.path.exists(p) else 0
    return out


def fmt(n):
    return f"{n / 1024 / 1024 / 1024:.3f} GB" if n > 100 * 1024 * 1024 else f"{n / 1024 / 1024:.2f} MB"


def print_sizes(label, s):
    print(f"{label}: db={fmt(s['db'])}  wal={fmt(s['wal'])}  shm={fmt(s['shm'])}")


def checkpoint(conn, mode):
    cur = conn.execute(f"PRAGMA wal_checkpoint({mode})")
    busy, log, checkpointed = cur.fetchone()
    return busy, log, checkpointed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--retries", type=int, default=5, help="TRUNCATE attempts before giving up (default 5)")
    ap.add_argument("--retry-delay", type=float, default=2.0, help="Seconds between retries (default 2)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: {args.db} not found. Run this from the folder containing it, or pass --db.")
        return

    print("=== Before ===")
    print_sizes("sizes", sizes(args.db))

    conn = sqlite3.connect(args.db, timeout=30)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    autockpt = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"journal_mode={journal_mode}  page_size={page_size}B  wal_autocheckpoint={autockpt} pages "
          f"(auto-checkpoint should fire every ~{autockpt * page_size / 1024 / 1024:.1f} MB of WAL growth)")

    print("\n=== PASSIVE checkpoint (never blocks) ===")
    busy, log, checkpointed = checkpoint(conn, "PASSIVE")
    print(f"busy={busy}  wal_frames_total={log}  frames_checkpointed={checkpointed}"
          + ("  <- some frames could NOT be checkpointed (a reader is holding an old snapshot)" if log != checkpointed else "  <- fully caught up"))

    print(f"\n=== TRUNCATE checkpoint (this is what actually shrinks the file; up to {args.retries} attempts) ===")
    for attempt in range(1, args.retries + 1):
        busy, log, checkpointed = checkpoint(conn, "TRUNCATE")
        print(f"  attempt {attempt}: busy={busy}  wal_frames_total={log}  frames_checkpointed={checkpointed}")
        if busy == 0:
            print("  -> succeeded, WAL should now be ~0 bytes.")
            break
        time.sleep(args.retry_delay)
    else:
        print("  -> still busy after all retries. Something is holding a connection open for "
              "multiple seconds at least -- not just the length of a single query. Suspects, in "
              "order of likelihood on a Windows VPS: (1) Windows Defender or Search Indexer scanning "
              "the -wal/-shm files (try excluding the db folder from real-time scanning), (2) a "
              "leaked/never-closed connection somewhere (check for any script or notebook left open "
              "against this db), (3) NSSM or another process holding a handle. Try stopping the "
              "TchouTchouIngest service and the API process one at a time and re-running this script "
              "after each to isolate which one (if either) is the blocker.")

    conn.close()

    print("\n=== After ===")
    print_sizes("sizes", sizes(args.db))


if __name__ == "__main__":
    main()
