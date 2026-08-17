#!/usr/bin/env python3
"""
Quick health check on a running or finished collection.

Usage:
    python stats.py --db tchoutchou.db
"""

import argparse
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="tchoutchou.db")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    def one(q, params=()):
        cur.execute(q, params)
        return cur.fetchone()

    print(f"Database: {args.db}\n")

    for feed in ("trip_updates", "service_alerts"):
        total = one("SELECT COUNT(*) FROM snapshots WHERE feed_name=?", (feed,))[0]
        ok = one("SELECT COUNT(*) FROM snapshots WHERE feed_name=? AND error IS NULL", (feed,))[0]
        span = one("SELECT MIN(fetched_at_utc), MAX(fetched_at_utc) FROM snapshots WHERE feed_name=?", (feed,))
        print(f"[{feed}] snapshots={total} ok={ok} errors={total - ok} span={span[0]} -> {span[1]}")

    tu_rows = one("SELECT COUNT(*) FROM trip_updates")[0]
    stu_rows = one("SELECT COUNT(*) FROM stop_time_updates")[0]
    alert_rows = one("SELECT COUNT(*) FROM service_alerts")[0]
    distinct_trips = one("SELECT COUNT(DISTINCT trip_id || start_date) FROM trip_updates")[0]
    log_errors = one("SELECT COUNT(*) FROM ingestion_log WHERE status='error'")[0]

    print(f"\ntrip_update rows: {tu_rows}")
    print(f"stop_time_update rows: {stu_rows}")
    print(f"service_alert rows: {alert_rows}")
    print(f"distinct (trip_id, start_date) combos seen: {distinct_trips}")
    print(f"failed poll attempts logged: {log_errors}")

    db_size_mb = one("SELECT page_count * page_size / 1024.0 / 1024.0 FROM pragma_page_count(), pragma_page_size()")[0]
    print(f"\ndatabase file size: {db_size_mb:.1f} MB")

    print("\nSample commercial_train_number values seen:")
    cur.execute("SELECT DISTINCT commercial_train_number FROM trip_updates WHERE commercial_train_number IS NOT NULL LIMIT 10")
    for row in cur.fetchall():
        print(f"  {row[0]}")

    print("\ntrain_type breakdown (only set when parse.py's confidence is high -- see SERVICE_CODE_INFO):")
    cur.execute(
        "SELECT COALESCE(train_type, service_code || ' (unmapped)'), COUNT(*) FROM trip_updates "
        "GROUP BY train_type, service_code ORDER BY COUNT(*) DESC LIMIT 20"
    )
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]}")

    cur.execute("SELECT COUNT(*) FROM stations")
    station_total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stations WHERE lookup_status='ok'")
    station_ok = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stations WHERE lookup_status='not_found'")
    station_not_found = cur.fetchone()[0]
    print(f"\nstation cache: {station_total} UIC codes resolved ({station_ok} found, {station_not_found} not found)")
    print("Sample resolved stations:")
    cur.execute("SELECT codes_uic, nom FROM stations WHERE lookup_status='ok' LIMIT 10")
    for row in cur.fetchall():
        print(f"  {row[0]} -> {row[1]}")


if __name__ == "__main__":
    main()
