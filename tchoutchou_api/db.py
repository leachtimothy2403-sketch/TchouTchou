"""
Read-only SQLite access for the TchouTchou product API, plus a handful of small helpers
duplicated (deliberately, not imported) from tchoutchou_ingest/ -- this API is a separate
deployable that only ever READS the collector's db, so it doesn't share a Python path
with tchoutchou_ingest/. Where the logic matters (UIC extraction, train nomenclature
fallback), it's copied verbatim from the ingest side with a comment pointing back to the
source of truth, rather than re-derived from scratch.

DB path: set TCHOUTCHOU_DB env var, or pass --db on the command line (see main.py).
Defaults to "tchoutchou.db" in the current directory, same convention as the ingest
scripts.
"""
import os
import re
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("TCHOUTCHOU_DB", "tchoutchou.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# Copied from tchoutchou_ingest/aggregate.py's extract_uic() -- confirmed (README.md,
# "Platform data" section) that both GTFS-RT stop_id and SIRI stop_point_ref end in a
# 5-8 digit UIC code despite otherwise different formats
# (StopPoint:OCE87481002 vs FR:ScheduledStopPoint::87734319), checked against all 3,589
# distinct refs in a live poll with 100% match. This is what lets the API line up a
# GTFS-RT stop with its SIRI platform call.
_STOP_UIC_RE = re.compile(r"(\d{5,8})\Z")


def extract_uic(stop_id):
    if not stop_id:
        return None
    m = _STOP_UIC_RE.search(stop_id)
    return m.group(1) if m else None


def short_cat(cat):
    # 'FR:TypeOfProductCategory::regionalRail::' -> 'regionalRail'
    if cat and "::" in cat:
        parts = [p for p in cat.split("::") if p]
        return parts[-1] if parts else cat
    return cat


def train_label(train_type, service_code, product_category_ref):
    """
    Same priority order as compare_platform_snapshots.py's _train_label() (see
    tchoutchou_ingest/compare_platform_snapshots.py, added 2026-08-19):
      1. GTFS-RT's mapped train_type (TER, OUIGO, TGV INOUI, ... -- parse.py's
         SERVICE_CODE_INFO, only set at "high confidence").
      2. the raw service_code, if GTFS-RT saw the train but it's not mapped yet.
      3. SIRI's own product_category_ref -- the only signal available for a train
         GTFS-RT never carried at all (common for Transilien/RER, see
         CROSS_VALIDATION_STUCK_SUMMARY.md).
    """
    if train_type:
        return train_type
    if service_code:
        return f"service_code {service_code} (unmapped)"
    if product_category_ref:
        return short_cat(product_category_ref)
    return None


def is_mission_code(train_number):
    """True for alphanumeric mission-code style train numbers (e.g. UMOL09, RER/Transilien
    style) that GTFS-RT's purely-numeric commercial_train_number can never carry -- see
    CROSS_VALIDATION_STUCK_SUMMARY.md. Coupled-unit pairs (126682-126683) are NOT mission
    codes -- either half can still match GTFS-RT."""
    if not train_number:
        return False
    if train_number.isdigit():
        return False
    parts = train_number.split("-")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return False
    return True


def station_name(conn, uic):
    if not uic:
        return None
    row = conn.execute("SELECT nom FROM stations WHERE codes_uic = ?", (uic,)).fetchone()
    return row["nom"] if row and row["nom"] else None
