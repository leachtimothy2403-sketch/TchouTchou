"""Checks for the 2026-10-06 mobile-readiness additions -- no network, no real SNCF key,
no real db. Builds a small synthetic db (trains/train_stats/stations), then exercises:
confidence tiers + Wilson intervals, train-type fallback, connection-risk fallback,
station autocomplete, app-key auth, rate limiting, CORS, /api/health, the SNCF daily
budget, and /api/search end to end with the SNCF call stubbed out.

    python test_mobile_api.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, ".")
DB = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["TCHOUTCHOU_DB"] = DB
os.environ["TCHOUTCHOU_CORS_ORIGINS"] = "http://localhost:8081"
os.environ.pop("TCHOUTCHOU_APP_KEYS", None)

c = sqlite3.connect(DB)
c.executescript("""
CREATE TABLE trains (train_number TEXT PRIMARY KEY, service_code TEXT, train_type TEXT,
  most_common_origin_uic TEXT, most_common_destination_uic TEXT,
  route_variant_count INTEGER DEFAULT 0, trips_observed INTEGER DEFAULT 0,
  first_seen_date TEXT, last_seen_date TEXT, updated_at_utc TEXT NOT NULL DEFAULT '');
CREATE TABLE train_stats (train_number TEXT, day_type TEXT, observations INTEGER,
  cancelled_count INTEGER, sum_final_delay INTEGER, sum_final_delay_sq INTEGER DEFAULT 0,
  on_time_count INTEGER, late_5_count INTEGER, late_15_count INTEGER, late_30_count INTEGER,
  updated_at_utc TEXT NOT NULL DEFAULT '2026-10-05T03:10:00Z',
  PRIMARY KEY (train_number, day_type));
CREATE TABLE stations (codes_uic TEXT PRIMARY KEY, nom TEXT, libellecourt TEXT,
  segment_drg TEXT, lon REAL, lat REAL, codeinsee TEXT, sncf_id TEXT,
  lookup_status TEXT NOT NULL, resolved_at_utc TEXT NOT NULL DEFAULT '', raw_json TEXT);
""")
# 6683: well-observed TGV (40 trips, 30 on time). 6742: TGV with 3 trips (insufficient).
# 87421: TER, 12 trips (emerging). 9999: ICE, 25 trips, 0 on time (provably bad).
trains = [("6683", "TGV INOUI"), ("6742", "TGV INOUI"), ("87421", "TER"), ("9999", "ICE"),
          ("5000", "TGV INOUI")]
for n, t in trains:
    c.execute("INSERT INTO trains (train_number, train_type, first_seen_date, last_seen_date) "
              "VALUES (?, ?, '20260817', '20261005')", (n, t))
stats = [  # n, day_type, obs, cancelled, sum_delay_s, on_time, late5, late15, late30
    ("6683", "weekday", 30, 1, 30 * 120, 23, 7, 3, 1),
    ("6683", "saturday", 10, 0, 10 * 60, 7, 3, 1, 0),
    ("6742", "weekday", 3, 0, 3 * 60, 3, 0, 0, 0),
    ("87421", "weekday", 12, 0, 12 * 90, 10, 2, 1, 0),
    ("9999", "weekday", 25, 2, 25 * 900, 0, 25, 15, 6),
    ("5000", "weekday", 200, 4, 200 * 180, 140, 60, 20, 8),
]
for row in stats:
    c.execute("INSERT INTO train_stats (train_number, day_type, observations, cancelled_count, "
              "sum_final_delay, on_time_count, late_5_count, late_15_count, late_30_count) "
              "VALUES (?,?,?,?,?,?,?,?,?)", row)
for uic, nom, code in [("87686006", "Paris Gare de Lyon", "PLY"),
                       ("87723197", "Lyon Part-Dieu", "LPD"),
                       ("87726000", "Saint-Étienne Châteaucreux", "SEC"),
                       ("87722025", "Lyon Perrache", "LPR")]:
    c.execute("INSERT INTO stations (codes_uic, nom, libellecourt, lookup_status) VALUES (?,?,?,'ok')",
              (uic, nom, code))
c.execute("INSERT INTO stations (codes_uic, nom, lookup_status) VALUES ('11111111', 'Ghost', 'not_found')")
c.commit()
c.close()

from fastapi.testclient import TestClient  # noqa: E402
import confidence  # noqa: E402
import main as m  # noqa: E402
import security  # noqa: E402
import sncf_journeys  # noqa: E402
import stations_search  # noqa: E402
from db import get_conn  # noqa: E402

client = TestClient(m.app)
ok = 0


def check(cond, label):
    global ok
    assert cond, label
    ok += 1
    print("OK  ", label)


# --- 1. Wilson interval sanity --------------------------------------------------
lo, hi = confidence.wilson_interval(0, 25)
check(lo == 0.0 and 12 < hi < 15, f"0/25 on time -> upper bound {hi}% (provably bad)")
lo, hi = confidence.wilson_interval(15, 20)
check(50 < lo < 55 and 88 < hi < 92, f"15/20 -> [{lo}, {hi}] (wide: rank is shaky)")
check(confidence.wilson_interval(0, 0) is None, "n=0 -> no interval")

# --- 2. Tiers + headline ---------------------------------------------------------
r = client.get("/api/trains/6683/reliability").json()
check(r["confidence"]["tier"] == "solid" and r["confidence"]["level"] == 3 and r["confidence"]["observations"] == 40, "40 trips -> solid (level 3 of 4)")
check(r["confidence"]["levels"] == 5 and r["confidence"]["thresholds"] == {"early": 5, "emerging": 10, "solid": 20, "high": 50}, "block carries level count + thresholds")
check(r["headline"]["source"] == "train" and r["headline"]["on_time_pct"] == 75.0, "solid -> headline from train itself")
check(r["by_day_type"]["saturday"]["confidence"]["tier"] == "emerging", "per-day-type tiers present")

r = client.get("/api/trains/6742/reliability").json()
check(r["confidence"]["tier"] == "insufficient", "3 trips -> insufficient")
check(r["headline"]["source"] == "train_type" and r["headline"]["train_type"] == "TGV INOUI",
      "insufficient -> headline falls back to train type")
tf = r["type_fallback"]
check(tf["observations"] == 243 and tf["n_trains"] == 3, "type fallback aggregates all TGV INOUI trains")
check(r["headline"]["tier"] == "high", "type-level headline carries its own (large-sample) tier")

r = client.get("/api/trains/87421/reliability").json()
check(r["confidence"]["tier"] == "emerging" and r["headline"]["source"] == "train", "12 trips -> emerging, own number")

r = client.get("/api/trains/9999/reliability").json()
check(r["overall"]["on_time_pct"] == 0.0 and r["confidence"]["on_time_ci95"][1] < 15, "ICE 0/25 flagged with tight upper bound")

r = client.get("/api/trains/424242/reliability").json()
check(r["available"] is False and r["headline"]["source"] is None, "unknown train -> no headline, not a crash")

# --- 2b. Tier boundaries, all five levels, env override ------------------------------
cases = [(0, "insufficient"), (4, "insufficient"), (5, "early"), (9, "early"), (10, "emerging"), (19, "emerging"),
         (20, "solid"), (49, "solid"), (50, "high"), (5000, "high")]
bad = [(n, confidence.tier_for(n), want) for n, want in cases if confidence.tier_for(n) != want]
check(not bad, f"tier boundaries at 4/5, 9/10, 19/20, 49/50 {bad or ''}")
check(confidence.tier_for(None) == "insufficient", "None observations -> insufficient")
os.environ["TCHOUTCHOU_TIER_THRESHOLDS"] = "3,6,12,30"
check(confidence.tier_for(12) == "solid" and confidence.tier_for(30) == "high", "thresholds overridable via env")
for broken in ("3,6,12", "10,5,20,50", "a,b,c,d", "0,5,10,20"):
    os.environ["TCHOUTCHOU_TIER_THRESHOLDS"] = broken
    check(confidence.thresholds() == confidence.DEFAULT_THRESHOLDS, f"malformed override {broken!r} falls back to defaults")
os.environ.pop("TCHOUTCHOU_TIER_THRESHOLDS")
r = client.get("/api/trains/9999/reliability").json()
check(r["confidence"]["tier"] == "solid" and r["headline"]["tier"] == "solid", "25 trips (ICE) -> solid")
r = client.get("/api/trains/5000/reliability").json()
check(r["confidence"]["tier"] == "high" and r["headline"]["tier"] == "high", "200 trips -> high")

# --- 3. Connection risk falls back to type history when the incoming train is thin ---
with get_conn() as conn:
    thin = m._reliability_summary(conn, "6742")
    p, note = m._connection_success_probability(thin, buffer_minutes=10)
expected = round(100 - tf["late_5_pct"], 1)
check(p == expected and note and "TGV INOUI" in note, f"thin incoming train -> type-level estimate {p}% with note")

with get_conn() as conn:
    hinted = m._reliability_summary(conn, "31337", type_hint="TER")
check(hinted["available"] is False and hinted["type_fallback"]["train_type"] == "TER",
      "never-seen train uses SNCF commercial_mode as a type hint")

# --- 4. Station autocomplete -----------------------------------------------------
def names(q):
    return [s["name"] for s in client.get("/api/stations", params={"q": q}).json()["results"]]

check(names("lyon part")[0] == "Lyon Part-Dieu", "prefix words match")
check("Paris Gare de Lyon" in names("gare lyon"), "word-order-insensitive match")
check(names("saint etienne") == ["Saint-Étienne Châteaucreux"], "accent-insensitive match")
check(names("LPD")[0] == "Lyon Part-Dieu", "trigram code match ranks first")
check(names("ghost") == [], "not_found rows excluded (and no SNCF call without a key)")
check(names("l") == [], "1-char query returns nothing")

# --- 5. /api/search end to end, SNCF stubbed --------------------------------------
def fake_search(o, d, date, t, count=5):
    return [
        {"origin": o, "destination": d, "departure_datetime": "20261010T180400",
         "arrival_datetime": "20261010T200200", "duration_seconds": 7080, "nb_transfers": 0,
         "status": None, "transfers": [],
         "legs": [{"train_number": "6683", "commercial_mode": "TGV INOUI", "operator": "SNCF",
                   "physical_mode": "Train grande vitesse", "headsign": "6683",
                   "from_station": o, "to_station": d,
                   "departure_datetime": "20261010T180400", "arrival_datetime": "20261010T200200",
                   "duration_seconds": 7080}]},
        {"origin": o, "destination": d, "departure_datetime": "20261010T175200",
         "arrival_datetime": "20261010T204100", "duration_seconds": 10140, "nb_transfers": 1,
         "status": None, "transfers": [{"at_station": "Lyon Part-Dieu", "buffer_minutes": 11}],
         "legs": [{"train_number": "6742", "commercial_mode": "TGV INOUI", "operator": "SNCF",
                   "physical_mode": "Train grande vitesse", "headsign": "6742",
                   "from_station": o, "to_station": "Lyon Part-Dieu",
                   "departure_datetime": "20261010T175200", "arrival_datetime": "20261010T191400",
                   "duration_seconds": 4920},
                  {"train_number": "87421", "commercial_mode": "TER", "operator": "SNCF",
                   "physical_mode": "TER", "headsign": "87421",
                   "from_station": "Lyon Part-Dieu", "to_station": d,
                   "departure_datetime": "20261010T192500", "arrival_datetime": "20261010T204100",
                   "duration_seconds": 4560}]},
    ]


real_search = sncf_journeys.search_journeys
sncf_journeys.search_journeys = fake_search
r = client.get("/api/search", params={"from": "Paris Gare de Lyon", "to": "Lyon Perrache",
                                      "date": "2026-10-10", "time": "18:00"})
check(r.status_code == 200, "search 200 with stubbed SNCF")
res = r.json()["results"]
check(res[0]["legs"][0]["reliability"]["headline"]["source"] == "train", "direct leg carries headline")
conn_it = res[1]
check(conn_it["transfers"][0]["connection_success_probability"] is not None, "thin first leg still gets a connection estimate")
check(conn_it["combined_success_probability"] is not None and conn_it["combined_probability_notes"],
      "combined probability present, fallback note surfaced")

# One comparable score: direct = the train's on-time %, change = connections x last train's on-time %
direct_score = res[0]["arrival_on_time_probability"]
check(direct_score == res[0]["legs"][0]["reliability"]["headline"]["on_time_pct"] == 75.0, "direct trip scored by its own on-time %")
last_on_time = conn_it["legs"][-1]["reliability"]["headline"]["on_time_pct"]
check(conn_it["arrival_on_time_probability"] == round(conn_it["combined_success_probability"] * last_on_time / 100, 1),
      "trip with a change = connection odds x last train's on-time %")
check(conn_it["arrival_on_time_probability"] < conn_it["combined_success_probability"], "...which is lower than connection odds alone (lateness counts)")
check(m._arrival_on_time_probability([], None) is None, "no legs -> None")
check(m._arrival_on_time_probability([{"reliability": None}], None) is None, "leg without data -> None, not a guess")
two = [{"reliability": None}, {"reliability": {"headline": {"on_time_pct": 80.0}}}]
check(m._arrival_on_time_probability(two, None) is None, "unknown connection risk -> whole score unknown")
check(m._arrival_on_time_probability(two, 50.0) == 40.0, "50% connections x 80% on time = 40%")

# Ranking by the cautious end: a known train beats a lucky-looking new one
with get_conn() as conn:
    known = m._reliability_summary(conn, "5000")  # 140/200 on time = 70%
    new = {"train_number": "n", "available": True, "overall": {"observations": 7, "on_time_pct": 85.7, "cancelled_count": 0},
           "confidence": confidence.confidence_block(7, 6), "type_fallback": None,
           "headline": {"source": "train", "on_time_pct": 85.7, "tier": "early", "observations": 7}}
s_known = m._ranking_score([{"reliability": known}], [])
s_new = m._ranking_score([{"reliability": new}], [])
check(s_known == known["confidence"]["on_time_ci95"][0] and s_new == new["confidence"]["on_time_ci95"][0],
      f"direct: ranking score = low end of the 95% range ({s_known} vs {s_new})")
check(s_known > s_new, "70% over 200 trips outranks 86% over 7 trips")
check(m._ranking_score([{"reliability": None}], []) is None, "no data -> no ranking score")
check(res[1]["ranking_score"] is not None and res[1]["ranking_score"] < res[1]["arrival_on_time_probability"],
      "trip with a change: ranking score present and more cautious than the displayed score")
check(m._connection_success_low(known, -3) == 0.0 and m._connection_success_low(known, None) is None,
      "impossible connection -> 0, unknown buffer -> None")
sncf_journeys.search_journeys = real_search

# --- 6. Health, CORS --------------------------------------------------------------
h = client.get("/api/health").json()
check(h["ok"] and h["stats_updated_at_utc"] and h["version"] == m.API_VERSION, "health reports data freshness")
pre = client.options("/api/search", headers={"Origin": "http://localhost:8081",
                                            "Access-Control-Request-Method": "GET",
                                            "Access-Control-Request-Headers": "x-app-key"})
check(pre.headers.get("access-control-allow-origin") == "http://localhost:8081", "CORS preflight allowed for Expo web")
bad = client.get("/api/health", headers={"Origin": "https://evil.example"})
check("access-control-allow-origin" not in bad.headers, "CORS not granted to other origins")

# --- 7. App key -------------------------------------------------------------------
os.environ["TCHOUTCHOU_APP_KEYS"] = "alpha-key-1,web-key-2"
check(client.get("/api/trains/6683/reliability").status_code == 401, "no key -> 401")
check(client.get("/api/trains/6683/reliability", headers={"X-App-Key": "nope"}).status_code == 401, "wrong key -> 401")
check(client.get("/api/trains/6683/reliability", headers={"X-App-Key": "alpha-key-1"}).status_code == 200, "valid key -> 200")
check(client.get("/api/trains/6683/reliability?key=web-key-2").status_code == 200, "?key= works for browser testing")
check(client.get("/api/health").status_code == 200, "health exempt from key")
check(client.get("/static/search.html").status_code == 200, "static pages not gated")

# --- 8. Rate limiting --------------------------------------------------------------
security.limiter.reset()
os.environ["TCHOUTCHOU_SEARCH_PER_HOUR"] = "3"
sncf_journeys.search_journeys = lambda *a, **k: []
hdr = {"X-App-Key": "alpha-key-1", "X-Device-Id": "device-aaaaaaaa"}
codes = [client.get("/api/search", params={"from": "a", "to": "b", "date": "2026-10-10"}, headers=hdr).status_code
         for _ in range(4)]
check(codes == [200, 200, 200, 429], f"4th search in the hour -> 429 ({codes})")
other = client.get("/api/search", params={"from": "a", "to": "b", "date": "2026-10-10"},
                   headers={**hdr, "X-Device-Id": "device-bbbbbbbb"})
check(other.status_code == 200, "limits are per device")
limited = client.get("/api/search", params={"from": "a", "to": "b", "date": "2026-10-10"}, headers=hdr)
check(limited.headers.get("retry-after") is not None, "429 carries Retry-After")
rotated = [client.get("/api/search", params={"from": "a", "to": "b", "date": "2026-10-10"},
                      headers={**hdr, "X-Device-Id": f"device-rot{i:05d}"}).status_code for i in range(12)]
check(429 in rotated, f"rotating device ids still hits the per-IP cap ({rotated.count(200)} allowed)")
sncf_journeys.search_journeys = real_search
os.environ.pop("TCHOUTCHOU_APP_KEYS")

# --- 9. SNCF daily budget ------------------------------------------------------------
os.environ["SNCF_API_KEY"] = "dummy"
os.environ["SNCF_DAILY_BUDGET"] = "2"
calls = []
sncf_journeys.requests.get = lambda *a, **k: calls.append(1) or type(
    "R", (), {"status_code": 200, "json": lambda self: {"places": []}, "text": ""})()
sncf_journeys._budget.update(day=None, used=0)
for _ in range(2):
    sncf_journeys._get("/x")
try:
    sncf_journeys._get("/x")
    check(False, "budget should block the 3rd call")
except sncf_journeys.SNCFAPIError as e:
    check("budget" in str(e) and len(calls) == 2, "3rd upstream call blocked by daily budget, never sent")

print(f"\nALL {ok} MOBILE-API CHECKS PASSED")
