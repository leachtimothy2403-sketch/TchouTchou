#!/usr/bin/env python3
"""
Cross-validates GTFS-RT's service_code-derived train_type against SIRI's line_name and
product_category_ref, joining trip_updates <-> platform_journeys on
(train_number, date). Two goals:

1. Decode the currently-unmapped GTFS-RT service codes (ICN, TRN, NA -- see parse.py's
   SERVICE_CODE_INFO) by seeing what line names they actually pair with in the wild.
   Motivating example: a manual spot-check (2026-08-19) found train UMOL09 is RER Ligne A
   and 164405 is Transilien Ligne N -- if service codes like TRN/NA turn out to
   consistently pair with RER/Transilien line names, that's a real, data-backed mapping
   ready to add to parse.py, not a guess.

2. Flag any ALREADY-mapped service_code whose line_name/product_category_ref looks
   inconsistent across trains -- a sign the mapping might be wrong for some slice of
   trains, not just incomplete. A clean, confidently-mapped code should show one
   dominant line_name/product_category_ref pattern, not several unrelated ones.

Usage:
    python cross_validate_train_type.py --db tchoutchou.db
    python cross_validate_train_type.py --db tchoutchou.db --min-count 3
"""
import argparse
import sqlite3
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--min-count", type=int, default=2,
                     help="Only print a line_name/product_category_ref variant seen at least this often (default 2)")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT DISTINCT tu.service_code, tu.train_type, pj.line_name, pj.product_category_ref, "
        "tu.commercial_train_number "
        "FROM trip_updates tu "
        "JOIN platform_journeys pj "
        "  ON pj.train_number = tu.commercial_train_number AND pj.calendar_date = tu.start_date "
        "WHERE tu.service_code IS NOT NULL"
    ).fetchall()

    matched_trains = len({r[4] for r in rows})
    total_trip_trains = cur.execute(
        "SELECT COUNT(DISTINCT commercial_train_number) FROM trip_updates WHERE commercial_train_number IS NOT NULL"
    ).fetchone()[0]
    pct = (matched_trains / total_trip_trains * 100) if total_trip_trains else 0
    print(f"Matched {matched_trains:,} of {total_trip_trains:,} distinct GTFS-RT train numbers to a same-day "
          f"SIRI journey ({pct:.1f}%). Unmatched trains aren't in this report -- either SIRI never observed "
          f"them, or the train_number format differs between feeds for some reason (worth checking if this "
          f"percentage is low).\n")

    by_code = defaultdict(lambda: defaultdict(int))
    train_type_by_code = {}
    for service_code, train_type, line_name, product_cat, _train in rows:
        by_code[service_code][(line_name, product_cat)] += 1
        train_type_by_code[service_code] = train_type

    unmapped = {"ICN", "TRN", "NA", None}
    for service_code in sorted(by_code, key=lambda c: -sum(by_code[c].values())):
        variants = by_code[service_code]
        total = sum(variants.values())
        mapped_label = train_type_by_code.get(service_code) or "UNMAPPED"
        flag = "  <-- currently unmapped in parse.py, decode this" if service_code in unmapped else ""
        print(f"service_code={service_code!r}  (train_type={mapped_label!r}, {total} matched trains){flag}")
        shown = 0
        for (line_name, product_cat), n in sorted(variants.items(), key=lambda kv: -kv[1]):
            if n < args.min_count:
                continue
            shown += 1
            print(f"    line_name={str(line_name):30s} product_category_ref={str(product_cat):20s}  n={n}")
        if not shown:
            print(f"    (no variant seen >= {args.min_count} times)")
        # Consistency check: if a code that's ALREADY mapped shows more than a couple
        # distinct (line_name, product_category_ref) pairs, that's worth a second look --
        # a clean mapping should be dominated by one pattern.
        if service_code not in unmapped and len(variants) > 3:
            print(f"    NOTE: {len(variants)} distinct line_name/product_category_ref variants for an "
                  f"already-mapped code -- worth checking this isn't masking a real split.")
        print()

    conn.close()


if __name__ == "__main__":
    main()
