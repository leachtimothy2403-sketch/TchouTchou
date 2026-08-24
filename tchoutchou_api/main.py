#!/usr/bin/env python3
"""
TchouTchou product API -- MVP. Reads the collector's SQLite db (read-only, never writes)
and serves the four features from the original pitch (see HANDOFF.md "What TchouTchou
is"): live status/tracking, confirmed/likely platform, a reliability score, and a
deep-link to SNCF Connect for booking. Crowd insights has no underlying data source yet
(see /trains/{train_number}/crowd) -- stubbed honestly rather than faked, see its
docstring.

Also serves /api/search (added 2026-08-24): real train search backed by SNCF's own
journey-planning API (see sncf_journeys.py), with each option annotated by TchouTchou's
reliability data -- needs SNCF_API_KEY set, see README.md "SNCF journey search setup".

Run (either works the same):
    pip install -r requirements.txt
    TCHOUTCHOU_DB=tchoutchou.db python main.py
    # or, equivalently:
    TCHOUTCHOU_DB=tchoutchou.db python -m uvicorn main:app --host 0.0.0.0 --port 8000

(PowerShell: `$env:TCHOUTCHOU_DB = "tchoutchou.db"; python main.py`)

Then open http://localhost:8000/ for the minimal UI, or hit the JSON endpoints directly
(e.g. http://localhost:8000/api/trains/9575/status). Ctrl+C to stop -- it runs in the
foreground; for an unattended run see "Deploying" in README.md.

Run this next to wherever the live db actually is (the VPS, for real-time data) -- see
tchoutchou_api/README.md.
"""
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from db import get_conn, extract_uic, train_label, is_mission_code, station_name
import sncf_journeys

app = FastAPI(title="TchouTchou API", version="0.1.0")

PARIS_TZ = ZoneInfo("Europe/Paris")


def _fmt_time(unix_ts):
    """arrival_time/departure_time on stop_time_updates are raw Unix timestamps (GTFS-RT
    StopTimeEvent.time, seconds since epoch, UTC) -- found 2026-08-19 that the API was
    never actually returning these at all, only the *_delay fields, and the UI labeled
    those "Arr."/"Dep." as if they were clock times. A delay of 0 (on time) then looked
    identical to "no data". Fixed to return the real formatted local time here."""
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(unix_ts, tz=PARIS_TZ).strftime("%H:%M")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_date(conn, train_number: str, date: Optional[str]) -> Optional[str]:
    """If a date wasn't given, pick the most recent one this train actually has data
    for -- checking trip_updates first, then platform_journeys (for SIRI-only/mission-code
    trains GTFS-RT never carries -- see is_mission_code())."""
    if date:
        return date
    row = conn.execute(
        "SELECT MAX(start_date) AS d FROM trip_updates WHERE commercial_train_number = ?",
        (train_number,),
    ).fetchone()
    if row and row["d"]:
        return row["d"]
    row = conn.execute(
        "SELECT MAX(calendar_date) AS d FROM platform_journeys WHERE train_number = ?",
        (train_number,),
    ).fetchone()
    return row["d"] if row else None


def _latest_trip_update(conn, train_number: str, date: str):
    return conn.execute(
        "SELECT id, trip_id, route_id, start_date, start_time, schedule_relationship, "
        "service_code, train_type, origin_uic, destination_uic, vehicle_label, "
        "trip_update_timestamp, trip_update_delay "
        "FROM trip_updates WHERE commercial_train_number = ? AND start_date = ? "
        "ORDER BY id DESC LIMIT 1",
        (train_number, date),
    ).fetchone()


def _platform_calls_by_uic(conn, train_number: str, date: str):
    """SIRI platform_calls keyed by UIC (extracted from stop_point_ref) instead of the
    raw ref, so it can be lined up against GTFS-RT's stop_time_updates -- see db.py's
    extract_uic() docstring for why that's a valid join key."""
    rows = conn.execute(
        "SELECT stop_point_ref, call_type, stop_point_name, "
        "aimed_arrival_time, expected_arrival_time, arrival_platform_name, "
        "aimed_departure_time, expected_departure_time, departure_platform_name "
        "FROM platform_calls WHERE train_number = ? AND calendar_date = ?",
        (train_number, date),
    ).fetchall()
    by_uic = {}
    for r in rows:
        uic = extract_uic(r["stop_point_ref"])
        if uic:
            by_uic[uic] = r
    return by_uic


def _platform_variant(conn, train_number: str, uic: str, call_field: str):
    """Best historical ('Likely') platform for a stop with no live confirmation yet --
    highest observed_count in platform_variants for this (train_number, ~stop, call_field).
    platform_variants.stop_point_ref is SIRI's raw ref; matched here by UIC suffix since
    that's the only stable join key across days (see db.py's extract_uic())."""
    rows = conn.execute(
        "SELECT stop_point_ref, platform_name, observed_count FROM platform_variants "
        "WHERE train_number = ? AND call_field = ?",
        (train_number, call_field),
    ).fetchall()
    candidates = [r for r in rows if extract_uic(r["stop_point_ref"]) == uic]
    if not candidates:
        return None
    best = max(candidates, key=lambda r: r["observed_count"])
    return {"platform_name": best["platform_name"], "observed_count": best["observed_count"]}


def _platform_status(conn, train_number: str, uic: str, call_field: str, call_row):
    """Confirmed -> Likely -> Unknown, per README's "Platform data" hierarchy."""
    plat_col = "arrival_platform_name" if call_field == "arrival" else "departure_platform_name"
    if call_row is not None and call_row["call_type"] == "recorded" and call_row[plat_col]:
        return {"status": "Confirmed", "platform": call_row[plat_col]}
    variant = _platform_variant(conn, train_number, uic, call_field)
    if variant:
        return {"status": "Likely", "platform": variant["platform_name"],
                "based_on_observations": variant["observed_count"]}
    return {"status": "Unknown", "platform": None}


# ---------------------------------------------------------------------------
# 1. Live status + tracking + platform
# ---------------------------------------------------------------------------

@app.get("/api/trains/{train_number}/status")
def train_status(train_number: str, date: Optional[str] = None):
    """
    Live delay, stop-by-stop itinerary ("live tracking" -- GTFS-RT trip_updates has no
    GPS position, only per-stop predictions; see the docstring note below), and
    Confirmed/Likely/Unknown platform per stop.

    For a train GTFS-RT never carries at all (common for Transilien/RER mission-code
    trains -- see CROSS_VALIDATION_STUCK_SUMMARY.md), this still returns whatever SIRI
    knows (platform calls, route), with gtfs_tracked=false and no delay/itinerary data,
    instead of a bare 404 -- that's a real, common case for this feed combination, not
    an error.
    """
    with get_conn() as conn:
        resolved_date = _resolve_date(conn, train_number, date)
        if resolved_date is None:
            raise HTTPException(status_code=404, detail=f"No data found for train {train_number}")

        tu = _latest_trip_update(conn, train_number, resolved_date)
        pj = conn.execute(
            "SELECT line_name, origin_name, destination_name, product_category_ref, "
            "origin_aimed_departure_time, destination_aimed_arrival_time "
            "FROM platform_journeys WHERE train_number = ? AND calendar_date = ?",
            (train_number, resolved_date),
        ).fetchone()

        label = train_label(
            tu["train_type"] if tu else None,
            tu["service_code"] if tu else None,
            pj["product_category_ref"] if pj else None,
        )

        result = {
            "train_number": train_number,
            "date": resolved_date,
            "label": label,
            "mission_code": is_mission_code(train_number),
            "gtfs_tracked": tu is not None,
            "origin": (pj["origin_name"] if pj else None),
            "destination": (pj["destination_name"] if pj else None),
            "line_name": pj["line_name"] if pj else None,
        }

        if tu is None:
            result["note"] = (
                "GTFS-RT has no record of this train for this date -- either it's a "
                "Transilien/RER-style service this feed doesn't cover (see "
                "CROSS_VALIDATION_STUCK_SUMMARY.md), or it just hasn't been polled yet. "
                "Platform info below (if any) comes from SIRI alone."
            )
            result["delay_minutes"] = None
            result["cancelled"] = None
            result["stops"] = []
        else:
            result["delay_minutes"] = (
                round(tu["trip_update_delay"] / 60) if tu["trip_update_delay"] is not None else None
            )
            result["cancelled"] = tu["schedule_relationship"] == "CANCELED"

            by_uic = _platform_calls_by_uic(conn, train_number, resolved_date)
            # NOTE (found 2026-08-19 while building this): stop_sequence came back NULL
            # for every row checked in a real sample -- SNCF's GTFS-RT feed apparently
            # doesn't populate it (it's optional in the GTFS-RT spec), not a parse.py bug.
            # Falls back to insertion order (id), which matches the feed's own stop list
            # order (protobuf repeated fields preserve order; parse.py inserts in the
            # order it reads them).
            stu_rows = conn.execute(
                "SELECT stop_sequence, stop_id, schedule_relationship, "
                "arrival_delay, arrival_time, departure_delay, departure_time "
                "FROM stop_time_updates WHERE trip_update_id = ? "
                "ORDER BY COALESCE(stop_sequence, id)",
                (tu["id"],),
            ).fetchall()

            stops = []
            for s in stu_rows:
                uic = extract_uic(s["stop_id"])
                call = by_uic.get(uic)
                stops.append({
                    "stop_sequence": s["stop_sequence"],
                    "station_uic": uic,
                    "station_name": station_name(conn, uic) or (call["stop_point_name"] if call else None),
                    "arrival_time": _fmt_time(s["arrival_time"]),
                    "arrival_delay_minutes": round(s["arrival_delay"] / 60) if s["arrival_delay"] is not None else None,
                    "departure_time": _fmt_time(s["departure_time"]),
                    "departure_delay_minutes": round(s["departure_delay"] / 60) if s["departure_delay"] is not None else None,
                    "schedule_relationship": s["schedule_relationship"],
                    "arrival_platform": _platform_status(conn, train_number, uic, "arrival", call) if uic else {"status": "Unknown", "platform": None},
                    "departure_platform": _platform_status(conn, train_number, uic, "departure", call) if uic else {"status": "Unknown", "platform": None},
                })
            result["stops"] = stops
            result["tracking_note"] = (
                "GTFS-RT trip_updates carries per-stop delay predictions, not a live GPS "
                "position -- there's no 'dot on a map' data source in this pipeline yet "
                "(would need a separate GTFS-RT VehiclePositions feed, not currently "
                "polled). 'stops' above is the live per-stop itinerary instead."
            )

        return result


# ---------------------------------------------------------------------------
# 2. Reliability score
# ---------------------------------------------------------------------------

def _reliability_summary(conn, train_number: str):
    """
    Punctuality stats from the permanent layer (train_stats, built daily by
    aggregate.py). Shared by the /reliability endpoint and /api/search's per-leg
    annotation -- pulled out (2026-08-24) so both use exactly the same computation
    instead of a second copy that could quietly drift from this one.

    README.md is explicit that this needs 2-4 weeks of history to mean anything --
    collection started 2026-08-17, so treat any number here as provisional until
    first_seen_date is old enough. days_of_history is always included so a caller can
    decide whether to show/hide the score rather than guessing.
    """
    train = conn.execute(
        "SELECT train_number, train_type, first_seen_date, last_seen_date, trips_observed "
        "FROM trains WHERE train_number = ?", (train_number,),
    ).fetchone()
    rows = conn.execute(
        "SELECT day_type, observations, cancelled_count, sum_final_delay, "
        "on_time_count, late_5_count, late_15_count, late_30_count "
        "FROM train_stats WHERE train_number = ?", (train_number,),
    ).fetchall()

    if not rows:
        return {
            "train_number": train_number,
            "available": False,
            "reason": "No aggregated history yet -- either aggregate.py hasn't processed "
                      "any trips for this train (needs a trip at least 1 day old), or this "
                      "train has never been observed. Try again once the collector has run "
                      "at least a day or two.",
        }

    days_of_history = None
    if train and train["first_seen_date"]:
        first = datetime.strptime(train["first_seen_date"], "%Y%m%d").date()
        last = datetime.strptime(train["last_seen_date"], "%Y%m%d").date() if train["last_seen_date"] else first
        days_of_history = (last - first).days + 1

    by_day_type = {}
    total = {"observations": 0, "cancelled_count": 0, "sum_final_delay": 0,
             "on_time_count": 0, "late_5_count": 0, "late_15_count": 0, "late_30_count": 0}
    for r in rows:
        obs = r["observations"]
        entry = {
            "observations": obs,
            "cancelled_count": r["cancelled_count"],
            "mean_delay_minutes": round(r["sum_final_delay"] / obs / 60, 1) if obs else None,
            "on_time_pct": round(r["on_time_count"] / obs * 100, 1) if obs else None,
            "late_5_pct": round(r["late_5_count"] / obs * 100, 1) if obs else None,
            "late_15_pct": round(r["late_15_count"] / obs * 100, 1) if obs else None,
            "late_30_pct": round(r["late_30_count"] / obs * 100, 1) if obs else None,
        }
        by_day_type[r["day_type"]] = entry
        for k in total:
            total[k] += r[k] or 0

    obs = total["observations"]
    overall = {
        "observations": obs,
        "cancelled_count": total["cancelled_count"],
        "mean_delay_minutes": round(total["sum_final_delay"] / obs / 60, 1) if obs else None,
        "on_time_pct": round(total["on_time_count"] / obs * 100, 1) if obs else None,
        "late_5_pct": round(total["late_5_count"] / obs * 100, 1) if obs else None,
        "late_15_pct": round(total["late_15_count"] / obs * 100, 1) if obs else None,
        "late_30_pct": round(total["late_30_count"] / obs * 100, 1) if obs else None,
    } if obs else None

    return {
        "train_number": train_number,
        "available": True,
        "days_of_history": days_of_history,
        "confidence_note": (
            "Based on limited data (< 2 weeks of collection) -- treat as provisional, "
            "not a stable reliability figure. See README.md 'Why 48 hours probably "
            "isn't enough'." if (days_of_history or 0) < 14 else None
        ),
        "overall": overall,
        "by_day_type": by_day_type,
    }


@app.get("/api/trains/{train_number}/reliability")
def train_reliability(train_number: str):
    with get_conn() as conn:
        return _reliability_summary(conn, train_number)


# ---------------------------------------------------------------------------
# 3. Crowd insights -- NOT built, honestly
# ---------------------------------------------------------------------------

@app.get("/api/trains/{train_number}/crowd")
def train_crowd(train_number: str):
    """
    No occupancy/crowding data source is polled by this pipeline at all -- neither
    GTFS-RT trip_updates nor SIRI ET Lite carry it (that needs something like SIRI-VM's
    OccupancyRef, or GTFS-RT VehiclePositions.occupancy_status, neither of which
    ingest.py fetches today). Returning a fabricated number here would be worse than
    returning nothing, so this is an explicit "not available" stub, not a stubbed-out
    fake value -- flagged in HANDOFF.md as a real product gap to close later, not
    forgotten.
    """
    return {
        "train_number": train_number,
        "available": False,
        "reason": "No crowding/occupancy data source is currently collected. Would need "
                  "a new feed (e.g. SIRI-VM OccupancyRef, or GTFS-RT VehiclePositions with "
                  "occupancy_status) wired into ingest.py before this can be real.",
    }


# ---------------------------------------------------------------------------
# 4. Deep-link to SNCF Connect
# ---------------------------------------------------------------------------

@app.get("/api/trains/{train_number}/deep_link")
def train_deep_link(train_number: str, date: Optional[str] = None):
    """
    Best-effort link out to SNCF Connect for booking. IMPORTANT: SNCF Connect's exact
    URL/query-param scheme for prefilling a search isn't confirmed against the live site
    (see README.md's legal section -- this pipeline is built entirely from the open-data
    feeds, not SNCF Connect's own web app), so `confirmed: false` and this should be
    manually checked in a browser before shipping to real users. Falls back to the plain
    homepage if origin/destination names aren't known.
    """
    with get_conn() as conn:
        resolved_date = _resolve_date(conn, train_number, date)
        pj = conn.execute(
            "SELECT origin_name, destination_name FROM platform_journeys "
            "WHERE train_number = ? AND calendar_date = ?",
            (train_number, resolved_date),
        ).fetchone() if resolved_date else None

    base = "https://www.sncf-connect.com/"
    if pj and pj["origin_name"] and pj["destination_name"]:
        # Best-effort guess at a search-prefill URL -- UNCONFIRMED, validate manually.
        url = (f"{base}app/home/search?origin={pj['origin_name']}"
               f"&destination={pj['destination_name']}")
        if resolved_date:
            url += f"&outwardDate={resolved_date}"
    else:
        url = base

    return {
        "train_number": train_number,
        "url": url,
        "confirmed": False,
        "note": "SNCF Connect's real query-param scheme hasn't been verified against the "
                "live site -- open this link and check it actually prefills the search "
                "before relying on it. If it doesn't, the safe fallback is linking to the "
                "homepage and asking the user to search manually.",
    }


# ---------------------------------------------------------------------------
# 5. Journey search -- real itineraries from SNCF's journey-planning API, annotated
#    with TchouTchou's own reliability data. Added 2026-08-24 to back the "search for a
#    ticket and see which option is most reliable" feature (see the search-results
#    mockup) -- previously that screen only had sample data.
# ---------------------------------------------------------------------------

def _connection_success_probability(reliability, buffer_minutes):
    """
    Rough estimate of P(the traveler makes this connection), grounded in the INCOMING
    leg's own delay-bucket history (train_stats' on_time/late_5/late_15/late_30 counts,
    which are cumulative -- "at least this late", see aggregate.py's bucket_counts())
    compared against the scheduled transfer buffer SNCF's API gave us.

    This is a first-pass approximation, not the full historical buffer-time model
    discussed in the product design conversation (which would need history keyed on the
    specific pair of trains at this specific transfer point, not just one leg's overall
    delay distribution). It answers "how often has this train historically been at least
    this late," which is a reasonable proxy but ignores correlation between the two legs
    (e.g. a shared disruption delaying both at once) -- flagged here, not hidden. Revisit
    if/when trip_finals' T-30/T-15/T-5 checkpoints (see tchoutchou_r2_storage.md) are
    deployed and give a finer-grained, transfer-time-aware signal to build this on.

    Returns (probability_pct_or_None, note_or_None).
    """
    if buffer_minutes is None:
        return None, "Transfer time unknown."
    if buffer_minutes < 0:
        return 0.0, "SNCF's schedule shows this connection as physically impossible (arrival after departure)."

    overall = (reliability or {}).get("overall")
    if not reliability or not reliability.get("available") or not overall or not overall.get("observations"):
        return None, "No reliability history yet for the incoming train."

    # late_5_pct == 100 - on_time_pct exactly (same 5-minute threshold on both sides, see
    # aggregate.py), so the <5min and <15min buffer cases collapse to the same bucket.
    if buffer_minutes < 15:
        p_miss = overall.get("late_5_pct")
    elif buffer_minutes < 30:
        p_miss = overall.get("late_15_pct")
    else:
        p_miss = overall.get("late_30_pct")

    if p_miss is None:
        return None, "Not enough delay-bucket history for this train yet."

    return round(max(0.0, min(100.0, 100 - p_miss)), 1), None


def _combined_probability(legs, transfers):
    """End-to-end probability for a multi-leg itinerary: the product of each transfer's
    connection-success probability and the final leg not being cancelled. Deliberately
    NOT the product of each leg's independent on-time percentage -- see the product
    design discussion (tchoutchou_monetization_mvp.md) on why that both misstates the
    real question (making the connection vs. each train individually being on time) and
    ignores correlated delays. Returns (probability_pct_or_None, list_of_notes).

    BUG FIXED 2026-08-24, found from a real SNCF response: an itinerary with an RER leg
    into a 4-minute transfer at Marne-la-Vallee-Chessy (RER legs never get a train_number,
    see sncf_journeys.py's _extract_train_number(), so there's no reliability history to
    estimate that transfer from) was coming back with combined_success_probability=100.0
    -- identical to a guaranteed direct train. The old code treated "unknown connection
    risk" as "skip this factor" (multiply by 1, i.e. certainty), which is backwards: a
    transfer we have zero data on is the LEAST certain thing in the itinerary, not the
    most. Now any transfer with an unknown connection-success probability makes the whole
    itinerary's combined probability None (unknown) rather than silently assuming success.
    """
    if not legs:
        return None, []

    notes = []
    p = 1.0

    for t in transfers:
        if t["connection_success_probability"] is None:
            if t.get("note"):
                notes.append(t["note"])
            return None, notes
        p *= t["connection_success_probability"] / 100

    have_cancellation_data = False
    last_leg_reliability = legs[-1]["reliability"]
    if last_leg_reliability and last_leg_reliability.get("available"):
        overall = last_leg_reliability.get("overall") or {}
        obs = overall.get("observations")
        cancelled = overall.get("cancelled_count")
        if obs:
            have_cancellation_data = True
            p *= 1 - (cancelled / obs)
    else:
        notes.append("No reliability/cancellation history yet for the final train.")

    if not transfers and not have_cancellation_data:
        return None, notes
    return round(p * 100, 1), notes


def _annotate_journey(conn, journey):
    legs = []
    for leg in journey["legs"]:
        reliability = _reliability_summary(conn, leg["train_number"]) if leg["train_number"] else None
        legs.append({**leg, "reliability": reliability})

    transfers = []
    for i, t in enumerate(journey["transfers"]):
        p_connect, note = _connection_success_probability(legs[i]["reliability"], t["buffer_minutes"])
        transfers.append({**t, "connection_success_probability": p_connect, "note": note})

    combined_probability, combined_notes = _combined_probability(legs, transfers)

    return {
        **{k: v for k, v in journey.items() if k not in ("legs", "transfers")},
        "legs": legs,
        "transfers": transfers,
        "combined_success_probability": combined_probability,
        "combined_probability_notes": combined_notes,
    }


@app.get("/api/search")
def search(
    from_: str = Query(..., alias="from", description="Origin station name, e.g. 'Paris Gare de Lyon'"),
    to: str = Query(..., description="Destination station name, e.g. 'Lyon Part-Dieu'"),
    date: str = Query(..., description="YYYY-MM-DD"),
    time: str = Query("08:00", description="HH:MM, requested departure time"),
    count: int = Query(5, ge=1, le=10),
):
    """
    Real train search: calls SNCF's journey-planning API (see sncf_journeys.py) for
    itinerary options between two stations, then annotates each leg with TchouTchou's own
    reliability data and a connection-risk-adjusted end-to-end probability for
    itineraries with a change. Backs the search-results screen (previously mockup-only
    sample data).

    Needs SNCF_API_KEY set -- see README.md "SNCF journey search setup". Returns 502 with
    a plain-English reason if the SNCF API call fails (missing/bad key, quota exhausted,
    unreachable, or an unknown station name) rather than a bare stack trace.

    CONFIRMED against a real response 2026-08-24 -- see sncf_journeys.py's module
    docstring. train_number extraction is correct. One real gap that run surfaced (now
    fixed): an RER/Transilien leg has no train_number (its headsign is a mission code,
    not a number), so it never has reliability history -- any transfer right after one
    used to silently show as 100% success. combined_success_probability for such
    itineraries now correctly comes back as null (unknown) instead. Legs still have
    reliability: null in that case -- there's just no data source for mission-code trains
    yet (a bigger feature than this fix; see README.md "Known gaps").
    """
    try:
        journeys = sncf_journeys.search_journeys(from_, to, date, time, count=count)
    except sncf_journeys.SNCFAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))

    with get_conn() as conn:
        results = [_annotate_journey(conn, j) for j in journeys]

    return {
        "from": from_,
        "to": to,
        "date": date,
        "time": time,
        "results": results,
        "combined_probability_methodology_note": (
            "combined_success_probability is a first-pass estimate based on each train's "
            "own historical delay-bucket distribution vs. the scheduled transfer time -- "
            "not a model of how this specific pair of trains has performed together at "
            "this specific transfer, and it doesn't account for correlated delays across "
            "legs (e.g. one disruption affecting both). Treat it as directional, not exact."
        ),
    }


# ---------------------------------------------------------------------------
# Combined endpoint (convenience for the minimal UI -- one fetch instead of four)
# ---------------------------------------------------------------------------

@app.get("/api/trains/{train_number}")
def train_all(train_number: str, date: Optional[str] = None):
    return {
        "status": train_status(train_number, date),
        "reliability": train_reliability(train_number),
        "crowd": train_crowd(train_number),
        "deep_link": train_deep_link(train_number, date),
    }


# ---------------------------------------------------------------------------
# Minimal UI
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    # Lets `python main.py` work directly, not just `python -m uvicorn main:app ...` --
    # running this file with plain `python main.py` used to do nothing (FastAPI needs an
    # ASGI server to actually serve requests; importing the file alone just builds the
    # `app` object and exits). Both ways of running it now work the same.
    import uvicorn
    import os as _os
    host = _os.environ.get("TCHOUTCHOU_API_HOST", "0.0.0.0")
    port = int(_os.environ.get("TCHOUTCHOU_API_PORT", "8000"))
    print(f"Starting TchouTchou API on http://{host}:{port} "
          f"(db: {_os.environ.get('TCHOUTCHOU_DB', 'tchoutchou.db')})")
    uvicorn.run(app, host=host, port=port)
