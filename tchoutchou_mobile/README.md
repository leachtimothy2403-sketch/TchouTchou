# TrainAware — mobile app (Expo / React Native)

iOS + Android app (and a web build for quick checks) on top of `../tchoutchou_api`.
TypeScript, Expo SDK 57, Expo Router. French and English.

## MVP screens

| Screen | What it does |
|---|---|
| **Search** | From / to with station autocomplete, day chips (next 14 days), time stepper. Defaults to the next half hour (rolls to tomorrow after midnight). |
| **Results** | Journeys sorted by *most reliable* / *fastest* / *earliest*; each card shows one comparable score (direct: on-time %; with changes: odds of making every change *and* arriving on time), a 5-level confidence badge and a "tight connection" warning. Star = save route. |
| **Journey detail** | Every train's track record (on-time %, 95% range, average delay, >15 min late, cancelled), each change with its own success odds, "Book on SNCF Connect" link-out. |
| **Train** | Look up a train number: live delay, cancellation, stop-by-stop times, platform per stop (*Confirmed*, or *Usually X* from history), and its track record. Also reachable from each train in a journey. |
| **Saved** | Saved routes, one tap to re-search. Stored on the phone, no account. |
| **Settings** | Language, server address, app key, *Test connection*. |

Honesty rules baked in: a number is never shown without its confidence tier; when there is
no data it says **Unknown** (never 0% or 100%); RER/Transilien legs have no history yet and
the app says so; prices and crowding are intentionally absent.

## Run it

```
cd tchoutchou_mobile
npm install
npx expo start          # scan the QR code with Expo Go (iOS/Android), or press w for web
```

Then **Settings → Server address**. Options:

1. *Try it with fake data* — on your PC: `cd ../tchoutchou_api && python dev_mock_server.py`,
   then use `http://<your PC's LAN IP>:8000` (phone and PC on the same Wi-Fi; `localhost`
   only works for the web build).
2. *Real data* — the VPS API behind a Cloudflare tunnel, see
   `../tchoutchou_api/deploy/README_TUNNEL.md`; enter the `https://….trycloudflare.com`
   address and the app key.

To pre-fill them at build time instead: `EXPO_PUBLIC_API_URL=… EXPO_PUBLIC_APP_KEY=… npx expo start`.

## Checks

```
npm test            # unit tests for ranking, tiers, time handling, i18n, API client
npm run typecheck
```

## Layout

```
src/app/            screens (Expo Router): (tabs)/index|saved|settings, results, journey
src/components/     Card, Button, ScoreMeter, TierBadge, JourneyCard, StationInput
src/lib/            api.ts (client), format.ts (ranking/time), i18n.ts, settings.tsx, theme.ts
```

## Not done yet

- Push alerts ("watch my train") — needs device-token storage and a notifier on the API.
- Store builds (`eas build`), icon/splash artwork (placeholders from the template), app
  name/bundle id review (`com.tchoutchou.trainaware` is a placeholder).
- Not yet run on a physical phone — verified as a web build in headless Chromium and by
  type-check/unit tests only.
