# Putting the API on the internet (free, no domain)

The phone app can't reach `localhost` on the VPS. A **Cloudflare quick tunnel** gives the
API a public `https://<random>.trycloudflare.com` address for free, with no account, no
domain and no inbound firewall rule (the VPS connects *out* to Cloudflare).

**Know the limits.** Cloudflare says quick tunnels are for *testing and development only*,
with no uptime guarantee, and the address **changes every time the tunnel restarts**. That
is fine for an alpha with a handful of testers. For anything public, buy a cheap domain
(~€10/year), move its DNS to Cloudflare and use a *named* tunnel (stable address, still
free) — the app and API need no code change, only the server address in Settings.

## One-time setup on the VPS

1. `git pull` (brings in `deploy/`, `confidence.py`, `security.py`, `stations_search.py`).
2. Install the new Python dependency set: `pip install -r requirements.txt`
3. Install cloudflared (PowerShell): `winget install --id Cloudflare.cloudflared`
   — or download `cloudflared-windows-amd64.exe` from
   https://github.com/cloudflare/cloudflared/releases, rename it `cloudflared.exe`, and put
   it on PATH. Open a **new** PowerShell afterwards.
4. In `tchoutchou_api\deploy\`: copy `api_env.example.bat` to `api_env.bat` and fill in the
   DB path, your SNCF key, and an app key (generate with
   `python -c "import secrets; print(secrets.token_urlsafe(24))"`).
   `api_env.bat` is git-ignored — keep it that way.

## Every time (two windows)

```
# window 1 -- the API
tchoutchou_api\deploy\start_api.bat

# window 2 -- the tunnel (prints the public URL and saves it to deploy\tunnel_url.txt)
powershell -ExecutionPolicy Bypass -File tchoutchou_api\deploy\start_tunnel.ps1
```

Check it from any device: open `<url>/api/health` — you should see `{"ok":true,...}`.
Then in the app: **Settings → Server address** = that URL, **App key** = your key →
*Test connection*.

## Keeping it running

For unattended running, wrap both scripts as services/scheduled tasks the same way
`TchouTchouIngest` is run (NSSM or Task Scheduler "at startup"). Start the API first.
Whenever the tunnel restarts, read the new address from `deploy\tunnel_url.txt` and update
the app's Settings.

## What protects the API once it is public

- **App key** (`TCHOUTCHOU_APP_KEYS`): every `/api/*` call except `/api/health` must send
  it. It stops casual bots; it is *not* a secret (it can be extracted from the app).
- **Rate limits**: 60 searches/hour per device (and 4x that per IP), 120 station lookups
  per minute — see `security.py`.
- **SNCF quota guard**: at most 4,500 real SNCF calls per day (`SNCF_DAILY_BUDGET`);
  repeated searches are served from a 5-minute cache. After that, searches return a clear
  "paused until midnight" message instead of burning the free tier.
- The API opens the database **read-only** and listens on 127.0.0.1 only; only the tunnel
  can reach it.
- Don't share the tunnel URL and key publicly. If they leak: change `TCHOUTCHOU_APP_KEYS`
  and restart (and restart the tunnel for a fresh URL).
