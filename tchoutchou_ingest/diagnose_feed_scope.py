#!/usr/bin/env python3
"""
Diagnoses WHY GTFS-RT <-> SIRI ET Lite train-number joins have a low match rate, instead
of iterating on another join-key theory. Companion to cross_validate_train_type.py --
run this FIRST; it answers "is the gap a broken join, a feed-scope difference, or a
train-identity semantics difference?" before trusting per-service_code numbers.

Rationale (see CROSS_VALIDATION_STUCK_SUMMARY.md for the full writeup and outside
advice that motivated this script): the 2.5%/23.7% match rates found so far conflate
several genuinely different populations of SIRI train_number:

  A. Direct numeric match candidates  (SIRI "164405" vs GTFS-RT "164405")
  B. Coupled-unit pairs               (SIRI "126682-126683" -- match if EITHER half
                                        is in GTFS-RT; not tried by cross_validate_train_type.py)
  C. Alphanumeric mission codes       (SIRI "UMOL09" -- structurally CANNOT match
                                        GTFS-RT's commercial_train_number, confirmed
                                        always pure-digit; not a bug, a real scope
                                        difference)

Only within bucket A is a truly "unmatched but should have matched" mystery possible.
This script:
  1. Splits SIRI's platform_journeys.train_number into buckets A/B/C for the given date
     (+/- --window days, to rule out date-join effects without requiring the same poll
     timestamp on both sides -- see CROSS_VALIDATION_STUCK_SUMMARY.md point 4).
  2. Reports match rate for A (direct) and B (either-half).
  3. Compares product_category_ref distribution between MATCHED and UNMATCHED numeric
     trains. If unmatched numeric trains are disproportionately regionalRail/
     suburbanRailway/local while matched trains skew highSpeedRail/regionalRail/
     interregionalRail, that's a scope-mismatch signature, not a join bug -- GTFS-RT's
     national feed and SIRI's coverage of dense regional/suburban (Transilien/RER-style)
     service are not the same universe.
  4. Prints top line_name values for the unmatched-numeric and alphanumeric buckets, to
     spot-check whether they're dominated by single-letter Transilien/RER line codes.

Usage:
    python diagnose_feed_scope.py --db tchoutchou.db
    python diagnose_feed_scope.py --db tchoutchou.db --date 20260819 --window 1
"""
import argparse
import sqlite3
from collections import Counter, defaultdict


def classify(train_number):
    if train_number is None:
        return "null"
    if train_number.isdigit():
        return "numeric"
    parts = train_number.split("-")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return "coupled_pair"
    return "alphanumeric"


def short_cat(cat):
    # 'FR:TypeOfProductCategory::regionalRail::' -> 'regionalRail'
    if cat and "::" in cat:
        parts = [p for p in cat.split("::") if p]
        return parts[-1] if parts else cat
    return cat or "(null)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="tchoutchou.db")
    ap.add_argument("--date", default=None,
                     help="GTFS-RT start_date to diagnose (YYYYMMDD). Default: the most recent date present.")
    ap.add_argument("--window", type=int, default=1,
                     help="Also pull SIRI journeys from +/- this many days around --date, to check "
                          "contemporaneity independent of the date-join logic itself (default 1).")
    ap.add_argument("--top-lines", type=int, default=15, help="How many top line_names to print per bucket")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cur = conn.cursor()

    date = args.date
    if date is None:
        row = cur.execute("SELECT MAX(start_date) FROM trip_updates WHERE start_date IS NOT NULL").fetchone()
        date = row[0]
        if date is None:
            print("No trip_updates data in this db.")
            return

    tu_trains = set(r[0] for r in cur.execute(
        "SELECT DISTINCT commercial_train_number FROM trip_updates "
        "WHERE commercial_train_number IS NOT NULL AND start_date = ?", (date,)
    ).fetchall())

    dates = [date]
    for offset in range(1, args.window + 1):
        for d in cur.execute("SELECT date(?, ?)", (
                f"{date[0:4]}-{date[4:6]}-{date[6:8]}", f"+{offset} day")).fetchone():
            dates.append(d.replace("-", ""))
        for d in cur.execute("SELECT date(?, ?)", (
                f"{date[0:4]}-{date[4:6]}-{date[6:8]}", f"-{offset} day")).fetchone():
            dates.append(d.replace("-", ""))

    qmarks = ",".join("?" for _ in dates)
    raw_rows = cur.execute(
        f"SELECT train_number, product_category_ref, line_name, calendar_date FROM platform_journeys "
        f"WHERE calendar_date IN ({qmarks})", dates
    ).fetchall()

    # Dedup defensively to one entry per distinct train_number -- the current (post
    # 2026-08-18) schema already guarantees this via PRIMARY KEY (train_number,
    # calendar_date), but an older append-only-schema db (pre-migration) can have many
    # rows per train per poll; take the most-common (category, line_name) seen, and
    # prefer the exact --date match over a +/-window one when both exist.
    per_train = defaultdict(Counter)
    exact_date_seen = set()
    for tn, cat, line, cal_date in raw_rows:
        per_train[tn][(cat, line)] += 1
        if cal_date == date:
            exact_date_seen.add(tn)

    pj_trains = {tn: c.most_common(1)[0][0] for tn, c in per_train.items()}

    print(f"Diagnosing {date} (SIRI window: {sorted(set(dates))})")
    print(f"GTFS-RT distinct trains on {date}: {len(tu_trains):,}")
    print(f"SIRI distinct trains in window: {len(pj_trains):,} "
          f"({len(exact_date_seen):,} on {date} exactly, {len(pj_trains) - len(exact_date_seen):,} only "
          f"in a neighboring day)\n")

    buckets = defaultdict(list)
    for tn, (cat, line) in pj_trains.items():
        buckets[classify(tn)].append((tn, cat, line))

    print("--- Bucket sizes ---")
    for k in ("numeric", "coupled_pair", "alphanumeric", "null"):
        print(f"  {k}: {len(buckets[k])}")
    print()

    # Bucket A: direct numeric match
    matched_numeric = [(tn, c, l) for tn, c, l in buckets["numeric"] if tn in tu_trains]
    unmatched_numeric = [(tn, c, l) for tn, c, l in buckets["numeric"] if tn not in tu_trains]
    tot = len(matched_numeric) + len(unmatched_numeric)
    print(f"--- Bucket A: direct numeric match ---")
    if tot:
        print(f"{len(matched_numeric):,}/{tot:,} matched ({len(matched_numeric)/tot*100:.1f}%), "
              f"{len(unmatched_numeric):,} unmatched ({len(unmatched_numeric)/tot*100:.1f}%)\n")

    # Bucket B: coupled pairs, match via either half
    matched_coupled = []
    unmatched_coupled = []
    for tn, c, l in buckets["coupled_pair"]:
        halves = tn.split("-")
        (matched_coupled if any(h in tu_trains for h in halves) else unmatched_coupled).append((tn, c, l))
    print(f"--- Bucket B: coupled-unit pairs (match via either half) ---")
    print(f"{len(matched_coupled)}/{len(buckets['coupled_pair'])} matched via at least one half, "
          f"{len(unmatched_coupled)} unmatched\n")

    # Bucket C: alphanumeric mission codes
    print(f"--- Bucket C: alphanumeric mission codes (structurally unmatchable) ---")
    print(f"{len(buckets['alphanumeric'])} trains -- these can never join GTFS-RT's pure-digit "
          f"commercial_train_number; not a bug.\n")

    def cat_dist(rows, label):
        c = Counter(short_cat(cat) for _, cat, _ in rows)
        total = sum(c.values())
        print(f"{label} (n={total}) product_category_ref distribution:")
        for cat, n in c.most_common():
            print(f"    {cat:25s} {n:6,d}  ({n/total*100:5.1f}%)")
        print()

    if matched_numeric:
        cat_dist(matched_numeric, "MATCHED numeric")
    if unmatched_numeric:
        cat_dist(unmatched_numeric, "UNMATCHED numeric  <-- the only genuinely unexplained bucket")
    if buckets["alphanumeric"]:
        cat_dist(buckets["alphanumeric"], "Alphanumeric mission codes")

    def line_sample(rows, label):
        c = Counter(line for _, _, line in rows if line)
        if not c:
            return
        print(f"Top line_name values, {label}:")
        for l, n in c.most_common(args.top_lines):
            print(f"    {l}: {n}")
        print()

    line_sample(unmatched_numeric, "UNMATCHED numeric")
    line_sample(buckets["alphanumeric"], "alphanumeric mission codes")

    print("--- Read on the result ---")
    print("If UNMATCHED numeric skews heavily toward regionalRail/suburbanRailway/local")
    print("relative to MATCHED numeric, and/or its line_names are dominated by single-letter")
    print("Transilien/RER-style codes (C, D, E, H, J, K, L, N, P, R, U), that's a feed-SCOPE")
    print("signature (SIRI covers Paris-region suburban rail that this GTFS-RT feed mostly")
    print("doesn't), not a join bug. Keep chasing a join-key fix only if the two distributions")
    print("look similar -- that would mean the join is failing on comparable trains, which IS")
    print("still a mystery worth solving.")

    conn.close()


if __name__ == "__main__":
    main()
