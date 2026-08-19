#!/usr/bin/env python3
"""
One-off diagnostic: does SIRI's platform_journeys.train_number contain alphanumeric
"mission code" style values (e.g. UMOL09, the RER/Transilien style) that GTFS-RT's
commercial_train_number structurally can never match, since parse.py's trip_id
extraction is regex-based and may only ever produce plain digit strings?

Usage: python check_mission_codes.py --db tchoutchou.db
"""
import argparse
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="tchoutchou.db")
args = ap.parse_args()

conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
cur = conn.cursor()

pj_trains = [r[0] for r in cur.execute("SELECT DISTINCT train_number FROM platform_journeys").fetchall()]
alnum_pj = [t for t in pj_trains if t and not t.isdigit()]
print(f"platform_journeys: {len(pj_trains)} distinct train_number, {len(alnum_pj)} are non-numeric (mission-code style)")
print("Examples:", alnum_pj[:10])
print()

tu_trains = [r[0] for r in cur.execute(
    "SELECT DISTINCT commercial_train_number FROM trip_updates WHERE commercial_train_number IS NOT NULL"
).fetchall()]
alnum_tu = [t for t in tu_trains if t and not t.isdigit()]
print(f"trip_updates: {len(tu_trains)} distinct commercial_train_number, {len(alnum_tu)} are non-numeric")
print("Examples:", alnum_tu[:10])
print()

if alnum_pj:
    print(f"{len(alnum_pj)} of {len(pj_trains)} SIRI trains ({len(alnum_pj)/len(pj_trains)*100:.1f}%) "
          f"use a non-numeric train_number -- these can only match GTFS-RT if commercial_train_number "
          f"is also ever non-numeric (see the trip_updates count above).")

# Split the "non-numeric" bucket into two genuinely different things: coupled-unit pairs
# like "126682-126683" (still plain digits either side of one hyphen -- potentially
# matchable by splitting on '-' and trying either half) vs true alphanumeric mission
# codes like "UMOL09" (RER/Transilien style -- structurally unmatchable against
# trip_updates, which never contains a letter at all per the count above).
hyphen_pairs = [t for t in alnum_pj if t.count("-") == 1 and all(p.isdigit() for p in t.split("-"))]
true_alnum = [t for t in alnum_pj if t not in hyphen_pairs]
print()
print(f"Of the {len(alnum_pj)} non-numeric train_number values:")
print(f"  {len(hyphen_pairs)} are coupled-unit pairs (digits-digits, e.g. {hyphen_pairs[:3] if hyphen_pairs else '(none)'})")
print(f"  {len(true_alnum)} are true alphanumeric mission codes (e.g. {true_alnum[:10] if true_alnum else '(none)'})")

conn.close()
