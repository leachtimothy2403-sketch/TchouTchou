"""
Light protection for exposing the API on the public internet (via Cloudflare Tunnel,
see deploy/README_TUNNEL.md). Two layers, both configured by environment variables so
local dev keeps working with nothing set:

1. App key -- TCHOUTCHOU_APP_KEYS="key1,key2". When set, every /api/* request (except
   /api/health) must send header `X-App-Key: <one of them>` (or `?key=` for quick
   browser testing). A key shipped inside a mobile app is NOT a secret -- anyone can
   pull it out of the bundle -- so this only stops casual scanners and bots that find
   the tunnel URL. Several keys are allowed so one can be rotated without breaking the
   other (e.g. "alpha build" vs "web pages").

2. Rate limits -- in-memory sliding windows per client. This is the real protection for
   the SNCF free tier (5,000 calls/day): /api/search is capped per client per hour, and
   sncf_journeys.py separately enforces a global daily budget on actual upstream calls.

Client identity: `X-Device-Id` header if the app sends one (stable per install), else
the caller's IP. Behind cloudflared every request arrives from 127.0.0.1, so the real
IP comes from Cloudflare's `CF-Connecting-IP` header -- only trusted when the direct
peer is localhost (i.e. the tunnel), so nobody can spoof it from outside.

In-memory = per process: fine for the single-instance MVP deployment, resets on restart.
"""
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

EXEMPT_PATHS = {"/api/health"}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def app_keys():
    raw = os.environ.get("TCHOUTCHOU_APP_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


# (path prefix, env var, default limit, window seconds) -- first match wins
RULES = [
    ("/api/search", "TCHOUTCHOU_SEARCH_PER_HOUR", 60, 3600),
    ("/api/stations", "TCHOUTCHOU_STATIONS_PER_MIN", 120, 60),
    ("/api/", "TCHOUTCHOU_API_PER_MIN", 120, 60),
]


class SlidingWindowLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key, limit, window):
        """Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(window - (now - q[0])) + 1)
            q.append(now)
            # opportunistic cleanup so idle clients don't accumulate forever
            if len(self._hits) > 50_000:
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True, 0

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


IP_MULTIPLIER = 4  # an IP may make this many times the per-device limit -- room for a
    # few phones behind one home/office/4G NAT, but rotating X-Device-Id on every request
    # can't get around the cap.


def client_ip(request: Request):
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip
    return peer


def client_keys(request: Request):
    """[(key, limit multiplier)] -- the device id (if sent) at 1x, plus the IP at
    IP_MULTIPLIER x; with no device id, the IP alone at 1x."""
    ip = client_ip(request)
    device = request.headers.get("x-device-id")
    if device and 8 <= len(device) <= 64:
        return [("dev:" + device, 1), ("ip:" + ip, IP_MULTIPLIER)]
    return [("ip:" + ip, 1)]


async def protect(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in EXEMPT_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    keys = app_keys()
    if keys:
        supplied = request.headers.get("x-app-key") or request.query_params.get("key")
        if supplied not in keys:
            return JSONResponse({"detail": "Missing or invalid app key."}, status_code=401)

    for prefix, env, default, window in RULES:
        if path.startswith(prefix):
            limit = _env_int(env, default)
            ok, retry = True, 0
            for cid, mult in client_keys(request):
                ok, retry = limiter.check((prefix, cid), limit * mult, window)
                if not ok:
                    break
            if not ok:
                return JSONResponse(
                    {"detail": "Too many requests -- please wait a moment and try again.",
                     "retry_after_seconds": retry},
                    status_code=429, headers={"Retry-After": str(retry)},
                )
            break

    return await call_next(request)
