"""
Station autocomplete for the search form (GET /api/stations?q=...).

Two sources, cheapest first:
  1. The collector's own `stations` table (every station a tracked train has called at,
     resolved from the SNCF open-data station list). Free -- no SNCF quota spent per
     keystroke. Matching is accent- and case-insensitive ("saint etienne" finds
     "Saint-Étienne Châteaucreux") and done in Python over an in-memory list, refreshed
     hourly; it's a few thousand rows.
  2. Only if the local table has no match: SNCF's /places autocomplete (via
     sncf_journeys, cached forever per query and counted against the daily budget).

The returned `name` is what the app passes back to /api/search as from/to, which
resolves it with SNCF's /places the same way it always has.
"""
import re
import threading
import time
import unicodedata

import sncf_journeys

REFRESH_SECONDS = 3600
_cache = {"at": 0.0, "rows": []}
_lock = threading.Lock()
_sncf_cache = {}


def normalize(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _load(conn):
    try:
        rows = conn.execute(
            "SELECT codes_uic, nom, libellecourt FROM stations "
            "WHERE lookup_status = 'ok' AND nom IS NOT NULL"
        ).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({
            "name": r["nom"],
            "uic": r["codes_uic"],
            "code": r["libellecourt"],
            "_norm": normalize(r["nom"]),
        })
    return out


def _local_rows(conn):
    now = time.time()
    with _lock:
        if now - _cache["at"] > REFRESH_SECONDS or not _cache["rows"]:
            _cache["rows"] = _load(conn)
            _cache["at"] = now
        return _cache["rows"]


def reset_cache():
    with _lock:
        _cache["at"] = 0.0
        _cache["rows"] = []
    _sncf_cache.clear()


def _score(norm_name, words, q_norm, code, q_raw):
    """Lower is better; None = no match. Every query word must prefix-match some word
    of the station name (so "lyon part" -> "Lyon Part-Dieu", "gare lyon" -> "Paris Gare
    de Lyon"). A trigram code match (e.g. "LPD") ranks first."""
    if code and q_raw.strip().upper() == code.upper():
        return -1
    name_words = norm_name.split()
    for w in words:
        if not any(nw.startswith(w) for nw in name_words):
            return None
    if norm_name.startswith(q_norm):
        return 0 + len(norm_name) / 1000
    return 1 + len(norm_name) / 1000


def search_local(conn, q, limit=8):
    q_norm = normalize(q)
    if len(q_norm) < 2:
        return []
    words = q_norm.split()
    scored = []
    for r in _local_rows(conn):
        s = _score(r["_norm"], words, q_norm, r["code"], q)
        if s is not None:
            scored.append((s, r))
    scored.sort(key=lambda x: x[0])
    return [{"name": r["name"], "uic": r["uic"], "code": r["code"], "source": "local"}
            for _, r in scored[:limit]]


def search_sncf(q, limit=8):
    key = normalize(q)
    if key in _sncf_cache:
        return _sncf_cache[key]
    data = sncf_journeys._get(
        f"/coverage/{sncf_journeys.COVERAGE}/places",
        params={"q": q, "count": limit, "type[]": "stop_area"},
    )
    out = [{"name": p.get("name"), "uic": None, "code": None, "source": "sncf"}
           for p in (data.get("places") or []) if p.get("name")]
    _sncf_cache[key] = out
    return out


def search(conn, q, limit=8):
    results = search_local(conn, q, limit)
    if results:
        return results
    if len(normalize(q)) < 3:
        return []
    try:
        return search_sncf(q, limit)
    except sncf_journeys.SNCFAPIError:
        return []  # autocomplete is best-effort; the search itself reports real errors
