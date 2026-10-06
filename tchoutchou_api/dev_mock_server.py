#!/usr/bin/env python3
"""
Run the real TchouTchou API against a small fake database and fake SNCF journeys, so the
mobile app (or static/search.html) can be tried end to end with NO SNCF key and NO real
tchoutchou.db. Nothing here touches your real data.

    python dev_mock_server.py            # http://localhost:8000
    python dev_mock_server.py --port 8010

Then in the app: Settings -> Server address -> http://<this-computer's-LAN-IP>:8000
(a phone can't reach "localhost"; use e.g. http://192.168.1.20:8000).

The fake data deliberately covers every UI state, including all five confidence tiers: a
high-confidence direct train, a 7-trip "early" one, a barely-observed train (3 trips) that
falls back to train-type stats, a provably bad train, a
connection that is comfortable, one that is tight, and an RER leg with no data at all
(-> "Unknown").
"""
import argparse
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE trains (train_number TEXT PRIMARY KEY, service_code TEXT, train_type TEXT,
  most_common_origin_uic TEXT, most_common_destination_uic TEXT,
  route_variant_count INTEGER DEFAULT 0, trips_observed INTEGER DEFAULT 0,
  first_seen_date TEXT, last_seen_date TEXT, updated_at_utc TEXT NOT NULL DEFAULT '');
CREATE TABLE train_stats (train_number TEXT, day_type TEXT, observations INTEGER,
  cancelled_count INTEGER, sum_final_delay INTEGER, sum_final_delay_sq INTEGER DEFAULT 0,
  on_time_count INTEGER, late_5_count INTEGER, late_15_count INTEGER, late_30_count INTEGER,
  updated_at_utc TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (train_number, day_type));
CREATE TABLE stations (codes_uic TEXT PRIMARY KEY, nom TEXT, libellecourt TEXT,
  segment_drg TEXT, lon REAL, lat REAL, codeinsee TEXT, sncf_id TEXT,
  lookup_status TEXT NOT NULL, resolved_at_utc TEXT NOT NULL DEFAULT '', raw_json TEXT);
-- live layer, only the columns tchoutchou_api reads (see main.py train_status)
CREATE TABLE trip_updates (id INTEGER PRIMARY KEY, trip_id TEXT, route_id TEXT,
  start_date TEXT, start_time TEXT, schedule_relationship TEXT, service_code TEXT,
  train_type TEXT, origin_uic TEXT, destination_uic TEXT, vehicle_label TEXT,
  trip_update_timestamp INTEGER, trip_update_delay INTEGER, commercial_train_number TEXT);
CREATE TABLE stop_time_updates (id INTEGER PRIMARY KEY, trip_update_id INTEGER,
  stop_sequence INTEGER, stop_id TEXT, schedule_relationship TEXT, arrival_delay INTEGER,
  arrival_time INTEGER, departure_delay INTEGER, departure_time INTEGER);
CREATE TABLE platform_journeys (train_number TEXT, calendar_date TEXT, line_name TEXT,
  origin_name TEXT, destination_name TEXT, product_category_ref TEXT,
  origin_aimed_departure_time TEXT, destination_aimed_arrival_time TEXT);
CREATE TABLE platform_calls (train_number TEXT, calendar_date TEXT, stop_point_ref TEXT,
  call_type TEXT, stop_point_name TEXT, aimed_arrival_time TEXT, expected_arrival_time TEXT,
  arrival_platform_name TEXT, aimed_departure_time TEXT, expected_departure_time TEXT,
  departure_platform_name TEXT);
CREATE TABLE platform_variants (train_number TEXT, stop_point_ref TEXT, call_field TEXT,
  platform_name TEXT, observed_count INTEGER);
"""

STATIONS = [
    ("87686006", "Paris Gare de Lyon", "PLY"), ("87723197", "Lyon Part-Dieu", "LPD"),
    ("87722025", "Lyon Perrache", "LPR"), ("87726000", "Saint-Étienne Châteaucreux", "SEC"),
    ("87686667", "Marne-la-Vallée Chessy", "MLC"), ("87751008", "Marseille Saint-Charles", "MSC"),
    ("87212027", "Strasbourg", "STG"), ("87581009", "Bordeaux Saint-Jean", "BDX"),
    ("87271007", "Lille Europe", "LLE"), ("87286005", "Lille Flandres", "LLF"),
]
TRAINS = [("6683", "TGV INOUI"), ("6689", "TGV INOUI"), ("6742", "TGV INOUI"),
          ("5000", "TGV INOUI"), ("87421", "TER"), ("9999", "ICE"), ("17001", "TER"),
          ("6801", "TGV INOUI")]
STATS = [  # train, day_type, obs, cancelled, sum_delay_s, on_time, late5, late15, late30
    ("6683", "weekday", 60, 1, 60 * 140, 46, 14, 5, 2),
    ("6689", "weekday", 24, 0, 24 * 300, 12, 12, 6, 2),
    ("6742", "weekday", 3, 0, 180, 3, 0, 0, 0),
    ("5000", "weekday", 400, 6, 400 * 170, 280, 120, 40, 14),
    ("87421", "weekday", 12, 0, 12 * 90, 10, 2, 1, 0),
    ("17001", "weekday", 900, 12, 900 * 120, 760, 140, 50, 12),
    ("9999", "weekday", 25, 2, 25 * 900, 0, 25, 15, 6),
    ("6801", "weekday", 7, 0, 7 * 100, 6, 1, 0, 0),
]


def build_db(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for n, t in TRAINS:
        c.execute("INSERT INTO trains (train_number, train_type, first_seen_date, last_seen_date) "
                  "VALUES (?,?,'20260817','20261005')", (n, t))
    for row in STATS:
        c.execute("INSERT INTO train_stats VALUES (?,?,?,?,?,0,?,?,?,?,?)", (*row, now))
    for uic, nom, code in STATIONS:
        c.execute("INSERT INTO stations (codes_uic, nom, libellecourt, lookup_status) VALUES (?,?,?,'ok')",
                  (uic, nom, code))
    _add_live_trains(c)
    c.commit()
    c.close()


def _add_live_trains(c):
    """Today's live data for the train-lookup screen, timed relative to *now* so it always
    looks in progress:
      6683  under way: left Paris 30 min ago on a confirmed platform, now +4 min, the
            usual platform at Lyon Part-Dieu (from history), nothing known for Perrache
      6689  cancelled today
      87421 SIRI-only (no GTFS-RT record): route + platform, no delays
    """
    now = int(datetime.now(timezone.utc).timestamp())
    today = datetime.now().strftime("%Y%m%d")
    sp = lambda uic: f"StopPoint:OCETrain TER-{uic}"          # GTFS-RT style, ends in UIC
    sr = lambda uic: f"FR:ScheduledStopPoint::{uic}"          # SIRI style, ends in UIC

    c.execute("INSERT INTO trip_updates (id, trip_id, start_date, schedule_relationship, train_type, "
              "trip_update_delay, commercial_train_number) VALUES (1, 'T6683', ?, 'SCHEDULED', 'TGV INOUI', 240, '6683')",
              (today,))
    stops = [  # uic, arr offset (s, from now), arr delay, dep offset, dep delay
        ("87686006", None, None, -30 * 60, 0),
        ("87723197", 84 * 60, 240, 89 * 60, 240),
        ("87722025", 98 * 60, 300, None, None),
    ]
    for i, (uic, a, ad, d, dd) in enumerate(stops):
        c.execute("INSERT INTO stop_time_updates (trip_update_id, stop_id, schedule_relationship, arrival_delay, "
                  "arrival_time, departure_delay, departure_time) VALUES (1, ?, 'SCHEDULED', ?, ?, ?, ?)",
                  (sp(uic), ad, now + a if a is not None else None, dd, now + d if d is not None else None))
    c.execute("INSERT INTO platform_journeys VALUES ('6683', ?, 'TGV INOUI', 'Paris Gare de Lyon', "
              "'Lyon Perrache', 'FR:TypeOfProductCategory::highSpeedRail::', NULL, NULL)", (today,))
    c.execute("INSERT INTO platform_calls (train_number, calendar_date, stop_point_ref, call_type, "
              "stop_point_name, departure_platform_name) VALUES ('6683', ?, ?, 'recorded', 'Paris Gare de Lyon', 'K')",
              (today, sr("87686006")))
    c.execute("INSERT INTO platform_calls (train_number, calendar_date, stop_point_ref, call_type, "
              "stop_point_name) VALUES ('6683', ?, ?, 'estimated', 'Lyon Part-Dieu')", (today, sr("87723197")))
    for field in ("arrival", "departure"):
        c.execute("INSERT INTO platform_variants VALUES ('6683', ?, ?, 'H', 41)", (sr("87723197"), field))
        c.execute("INSERT INTO platform_variants VALUES ('6683', ?, ?, 'G', 6)", (sr("87723197"), field))

    c.execute("INSERT INTO trip_updates (id, trip_id, start_date, schedule_relationship, train_type, "
              "trip_update_delay, commercial_train_number) VALUES (2, 'T6689', ?, 'CANCELED', 'TGV INOUI', NULL, '6689')",
              (today,))
    c.execute("INSERT INTO platform_journeys VALUES ('6689', ?, 'TGV INOUI', 'Paris Gare de Lyon', "
              "'Lyon Part-Dieu', NULL, NULL, NULL)", (today,))

    c.execute("INSERT INTO platform_journeys VALUES ('87421', ?, 'TER', 'Lyon Part-Dieu', "
              "'Saint-Étienne Châteaucreux', 'FR:TypeOfProductCategory::regionalRail::', NULL, NULL)", (today,))
    c.execute("INSERT INTO platform_calls (train_number, calendar_date, stop_point_ref, call_type, "
              "stop_point_name, departure_platform_name) VALUES ('87421', ?, ?, 'recorded', 'Lyon Part-Dieu', 'B')",
              (today, sr("87723197")))


def _fmt(d):
    return d.strftime("%Y%m%dT%H%M%S")


def _leg(num, mode, a, b, dep, mins):
    return {"train_number": num, "commercial_mode": mode, "operator": "SNCF", "physical_mode": mode,
            "headsign": num or "QIDO", "from_station": a, "to_station": b,
            "departure_datetime": _fmt(dep), "arrival_datetime": _fmt(dep + timedelta(minutes=mins)),
            "duration_seconds": mins * 60}


def fake_search(origin, destination, date, time_str, count=5):
    start = datetime.strptime(f"{date.replace('-', '')} {time_str}", "%Y%m%d %H:%M")
    mid = "Lyon Part-Dieu"
    out = []

    def journey(legs, transfers):
        return {"origin": origin, "destination": destination,
                "departure_datetime": legs[0]["departure_datetime"], "arrival_datetime": legs[-1]["arrival_datetime"],
                "duration_seconds": int((datetime.strptime(legs[-1]["arrival_datetime"], "%Y%m%dT%H%M%S")
                                         - datetime.strptime(legs[0]["departure_datetime"], "%Y%m%dT%H%M%S")).total_seconds()),
                "nb_transfers": len(transfers), "status": None, "legs": legs, "transfers": transfers}

    d1 = start + timedelta(minutes=4)
    out.append(journey([_leg("6683", "TGV INOUI", origin, destination, d1, 118)], []))
    d2 = start + timedelta(minutes=34)
    out.append(journey([_leg("6689", "TGV INOUI", origin, destination, d2, 121)], []))
    d3 = start + timedelta(minutes=50)
    a = _leg("6742", "TGV INOUI", origin, mid, d3, 82)
    b = _leg("87421", "TER", mid, destination, d3 + timedelta(minutes=82 + 25), 70)
    out.append(journey([a, b], [{"at_station": mid, "buffer_minutes": 25}]))
    d4 = start + timedelta(minutes=70)
    a = _leg("6683", "TGV INOUI", origin, mid, d4, 112)
    b = _leg("17001", "TER", mid, destination, d4 + timedelta(minutes=112 + 6), 55)
    out.append(journey([a, b], [{"at_station": mid, "buffer_minutes": 6}]))
    d5 = start + timedelta(minutes=95)
    a = _leg(None, "RER", origin, "Marne-la-Vallée Chessy", d5, 40)
    b = _leg("5000", "TGV INOUI", "Marne-la-Vallée Chessy", destination, d5 + timedelta(minutes=44), 130)
    out.append(journey([a, b], [{"at_station": "Marne-la-Vallée Chessy", "buffer_minutes": 4}]))
    d6 = start + timedelta(minutes=120)
    out.append(journey([_leg("9999", "ICE", origin, destination, d6, 240)], []))
    d7 = start + timedelta(minutes=140)
    out.append(journey([_leg("6801", "TGV INOUI", origin, destination, d7, 125)], []))
    return out  # ignores `count`: a mock should always show every UI state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    db = os.path.join(tempfile.mkdtemp(prefix="tchoutchou_mock_"), "mock.db")
    build_db(db)
    os.environ["TCHOUTCHOU_DB"] = db
    os.environ.setdefault("TCHOUTCHOU_CORS_ORIGINS", "http://localhost:8081,http://localhost:19006,http://localhost:8099")

    import sncf_journeys
    sncf_journeys.search_journeys = fake_search  # no SNCF key / network needed
    import main as api
    import uvicorn

    print(f"MOCK API on http://{args.host}:{args.port}  (fake db: {db})")
    print("Try: /api/health, /api/stations?q=lyon, /api/search?from=Paris Gare de Lyon&to=Lyon Perrache&date=2026-10-12&time=08:00")
    print("Train lookup: 6683 (running, +4 min), 6689 (cancelled), 87421 (no live delays), 424242 (not found)")
    uvicorn.run(api.app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
