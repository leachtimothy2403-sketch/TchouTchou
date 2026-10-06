@echo off
REM Copy this file to api_env.bat (same folder) and fill it in. api_env.bat holds secrets
REM and is git-ignored -- never commit it.

REM Path to the live collector database on this VPS
set TCHOUTCHOU_DB=C:\TchouTchou\tchoutchou_ingest\tchoutchou.db

REM Free SNCF journey-planning key (https://numerique.sncf.com/startup/api/token-developpeur/)
set SNCF_API_KEY=PASTE_YOUR_SNCF_KEY_HERE

REM App key(s) the mobile app must send. Generate one with:
REM     python -c "import secrets; print(secrets.token_urlsafe(24))"
REM Comma-separate several to rotate keys without breaking installed apps.
REM (A key inside a mobile app is not a true secret -- it only keeps casual scanners out.)
set TCHOUTCHOU_APP_KEYS=PASTE_A_GENERATED_KEY_HERE

REM Optional tuning (defaults shown)
REM set TCHOUTCHOU_SEARCH_PER_HOUR=60
REM set SNCF_DAILY_BUDGET=4500
