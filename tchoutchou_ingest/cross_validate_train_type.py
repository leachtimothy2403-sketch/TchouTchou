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

2. Flag any ALREADY-mapped service_code whose product_category_ref looks inconsistent
   across trains -- a sign the mapping might be wrong for some slice of trains, not just
   incomplete. line_name is deliberately NOT used for this check -- a broad category
   like TER legitimately spans dozens of real regional lines, so line_name diversity is
   expected and not a signal of anything wrong. product_category_ref should still be
   consistent within one service_code even when line_name varies a lot.

Date join note: GTFS-RT's start_date follows GTFS convention -- a trip that runs past
midnight keeps the service day it STARTED on, not the calendar date the stop falls on.
SIRI's calendar_date is a real calendar date. So a train departing 23:50 and arriving
01:30 has start_date = yesterday but calendar_date = today, and would silently miss an
exact-date join. --loose-date (default on) also tries calendar_date = start_date + 1 day
to catch these; use --no-loose-date to see the strict-match-only count for comparison.

Usage:
    python cross_validate_train_type.py --db tchoutchou.db
    python cross_validate_train_type.py --db tchoutchou.db --min-count 3
    python cross_validate_train_type.py --db tchoutchou.db --no-loose-date
"""
import argparse
import sqlite3
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--min-count", type=int, default=2,
                     help="Only print a line_name/product_category_ref variant seen at least this often (default 2)")
    ap.add_argument("--loose-date", dest="loose_date", action="store_true", default=True,
                     help="Also match calendar_date = start_date + 1 day, to catch trains that cross midnight (default on)")
    ap.add_argument("--no-loose-date", dest="loose_date", action="store_false",
                     help="Strict date match only (calendar_date = start_date)")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = conn.cursor()

    date_condition = (
        "(pj.calendar_date = tu.start_date OR pj.calendar_date = date(tu.start_date, '+1 day'))"
        if args.loose_date else
        "pj.calendar_date = tu.start_date"
    )
    rows = cur.execute(
        "SELECT DISTINCT tu.service_code, tu.train_type, pj.line_name, pj.product_category_ref, "
        "tu.commercial_train_number "
        "FROM trip_updates tu "
        "JOIN platform_journeys pj "
        f"  ON pj.train_number = tu.commercial_train_number AND {date_condition} "
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
    product_cat_counts_by_code = defaultdict(lambda: defaultdict(int))
    train_type_by_code = {}
    for service_code, train_type, line_name, product_cat, _train in rows:
        by_code[service_code][(line_name, product_cat)] += 1
        product_cat_counts_by_code[service_code][product_cat] += 1
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
        # Consistency check keyed on product_category_ref ONLY, not line_name -- a broad
        # category (TER, CTE) legitimately spans many real lines, so line_name diversity
        # is expected and not a signal of anything wrong. product_category_ref should
        # still be a single dominant value even when line_name varies a lot; several
        # product_category_ref values for one service_code is the real inconsistency
        # signal (e.g. OUIGO's real highSpeedRail/local split from its two sub-brands is
        # borderline-expected; a code showing 3+ unrelated categories is worth a look).
        cat_counts = product_cat_counts_by_code[service_code]
        if service_code not in unmapped and len(cat_counts) > 1:
            cat_summary = ", ".join(f"{cat}={n}" for cat, n in sorted(cat_counts.items(), key=lambda kv: -kv[1]))
            print(f"    NOTE: product_category_ref varies for an already-mapped code: {cat_summary} "
                  f"-- worth checking this isn't masking a real split (line_name diversity alone is normal "
                  f"and not flagged).")
        print()

    conn.close()


if __name__ == "__main__":
    main()
