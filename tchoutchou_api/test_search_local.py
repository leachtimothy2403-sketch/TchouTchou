"""Quick local sanity check -- no network, no real SNCF key. Exercises the parsing and
connection-risk logic against a hand-built Navitia-shaped response and synthetic
reliability data, mirroring the mockup's TGV 6683 example."""
import sys
sys.path.insert(0, ".")

from sncf_journeys import _parse_journey
import main as m


def fake_journey_direct():
    return {
        "duration": 7080,
        "departure_date_time": "20260825T180400",
        "arrival_date_time": "20260825T200200",
        "nb_transfers": 0,
        "status": "",
        "sections": [
            {
                "type": "public_transport",
                "display_informations": {"headsign": "6683", "network": "SNCF", "physical_mode": "TGV"},
                "from": {"name": "Paris Gare de Lyon"},
                "to": {"name": "Lyon Part-Dieu"},
                "departure_date_time": "20260825T180400",
                "arrival_date_time": "20260825T200200",
                "duration": 7080,
            }
        ],
    }


def fake_journey_connecting():
    return {
        "duration": 10140,
        "departure_date_time": "20260825T175200",
        "arrival_date_time": "20260825T204100",
        "nb_transfers": 1,
        "status": "",
        "sections": [
            {
                "type": "public_transport",
                "display_informations": {"headsign": "87421", "network": "SNCF", "physical_mode": "TER"},
                "from": {"name": "Paris Gare de Lyon"},
                "to": {"name": "Lyon Part-Dieu"},
                "departure_date_time": "20260825T175200",
                "arrival_date_time": "20260825T191400",
                "duration": 4920,
            },
            {
                "type": "public_transport",
                "display_informations": {"headsign": "6742", "network": "SNCF", "physical_mode": "TGV"},
                "from": {"name": "Lyon Part-Dieu"},
                "to": {"name": "Lyon Perrache"},
                "departure_date_time": "20260825T192500",
                "arrival_date_time": "20260825T204100",
                "duration": 4560,
            },
        ],
    }


def fake_reliability(on_time, late5, late15, late30, cancelled=1, obs=100):
    return {
        "train_number": "x",
        "available": True,
        "days_of_history": 30,
        "overall": {
            "observations": obs,
            "cancelled_count": cancelled,
            "on_time_pct": on_time,
            "late_5_pct": late5,
            "late_15_pct": late15,
            "late_30_pct": late30,
        },
        "by_day_type": {},
    }


# 1. Parse a direct journey
j = _parse_journey(fake_journey_direct(), "Paris Gare de Lyon", "Lyon Part-Dieu")
assert len(j["legs"]) == 1
assert j["legs"][0]["train_number"] == "6683", j["legs"][0]
assert j["transfers"] == []
print("direct journey parse: OK ->", j["legs"][0]["train_number"], j["duration_seconds"])

# 2. Parse a connecting journey, check transfer buffer computed correctly (19:14 -> 19:25 = 11 min)
j2 = _parse_journey(fake_journey_connecting(), "Paris Gare de Lyon", "Lyon Perrache")
assert len(j2["legs"]) == 2
assert j2["legs"][0]["train_number"] == "87421"
assert j2["legs"][1]["train_number"] == "6742"
assert len(j2["transfers"]) == 1
assert j2["transfers"][0]["buffer_minutes"] == 11, j2["transfers"][0]
print("connecting journey parse: OK -> transfer buffer", j2["transfers"][0]["buffer_minutes"], "min")

# 3. Connection-risk estimator: good on-time leg, generous buffer -> high success prob
rel_good = fake_reliability(on_time=85, late5=15, late15=6, late30=2)
p, note = m._connection_success_probability(rel_good, buffer_minutes=11)
assert note is None
assert p == 85.0, p  # buffer 11 < 15 -> uses late_5_pct (15) -> 100-15=85
print("connection prob (11 min buffer, good leg): OK ->", p, "%")

# 4. Tight buffer, unreliable leg -> low success prob
rel_bad = fake_reliability(on_time=40, late5=60, late15=35, late30=10)
p2, note2 = m._connection_success_probability(rel_bad, buffer_minutes=8)
assert p2 == 40.0, p2  # 100 - late_5_pct(60) = 40
print("connection prob (8 min buffer, unreliable leg): OK ->", p2, "%")

# 5. No reliability history yet -> None with a note, not a crash
p3, note3 = m._connection_success_probability(None, buffer_minutes=11)
assert p3 is None and note3
print("connection prob (no history): OK ->", note3)

# 6. Combined probability across two legs+transfer, matches manual product
legs = [
    {"reliability": rel_good, "train_number": "87421"},
    {"reliability": fake_reliability(on_time=88, late5=12, late15=4, late30=1, cancelled=2, obs=100),
     "train_number": "6742"},
]
transfers = [{"connection_success_probability": 85.0, "note": None}]
combined, notes = m._combined_probability(legs, transfers)
expected = round(0.85 * (1 - 2 / 100) * 100, 1)  # transfer success * (1 - final-leg cancellation rate)
assert combined == expected, (combined, expected)
print("combined probability: OK ->", combined, "% (expected", expected, "%)")

# 7. Regression check for the 2026-08-24 bug: a transfer with UNKNOWN connection risk
# (e.g. an incoming RER leg with no train_number, so no reliability history) must make
# the whole itinerary's combined probability None/unknown, NOT silently 100%. Found from
# a real SNCF response where an RER->TGV change at Marne-la-Vallee-Chessy came back
# showing combined_success_probability=100.0 despite an explicit "no history" note.
legs_unknown_transfer = [
    {"reliability": None, "train_number": None},  # RER leg: no train_number -> no reliability
    {"reliability": fake_reliability(on_time=88, late5=12, late15=4, late30=1, cancelled=0, obs=6),
     "train_number": "9844"},
]
transfers_unknown = [{"connection_success_probability": None, "note": "No reliability history yet for the incoming train."}]
combined2, notes2 = m._combined_probability(legs_unknown_transfer, transfers_unknown)
assert combined2 is None, combined2  # must NOT be 100.0
assert notes2 == ["No reliability history yet for the incoming train."], notes2
print("combined probability (unknown transfer): OK -> correctly None, not a false 100%")

print("\nALL LOCAL CHECKS PASSED")
