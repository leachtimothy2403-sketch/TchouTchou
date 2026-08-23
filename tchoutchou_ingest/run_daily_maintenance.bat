@echo off
rem Replaces the separate run_aggregate.bat / run_purge.bat Task Scheduler jobs (see
rem HANDOFF.md) with a single, strictly-sequential pipeline:
rem
rem   aggregate.py -> repair_mismatches.py --apply -> verify_aggregate.py -> purge_raw.py
rem
rem Why one script instead of two scheduled 15 minutes apart: repair_mismatches.py and
rem verify_aggregate.py both need raw data that's still present -- running them as
rem separate scheduled tasks would leave a race where purge_raw.py could start deleting
rem rows mid-run on a day aggregate.py runs long. Chaining them in one script guarantees
rem purge never starts until repair/verify have finished with that day's data.
rem
rem repair_mismatches.py --apply is unconditional and safe to run every night: it's a
rem no-op ("already correct") on days nothing needs fixing, and only ever touches a key
rem when it can prove every contributing trip's raw data is still present.
rem
rem verify_aggregate.py exits non-zero whenever it finds ANY hard mismatch -- including
rem the known, accepted station-87640912 cluster (see tchoutchou_r2_storage.md), which
rem will keep flagging every night until that's fixed. That's expected, not a failure, so
rem its exit code is logged but does NOT abort the pipeline -- only aggregate.py or
rem repair_mismatches.py actually crashing does that, to avoid purging raw data after an
rem incomplete/failed run.

cd /d C:\TchouTchou\tchoutchou_ingest

echo ============================================== >> daily_maintenance.log
echo %date% %time% - starting daily maintenance >> daily_maintenance.log

.venv\Scripts\python.exe aggregate.py --db tchoutchou.db >> daily_maintenance.log 2>&1
if errorlevel 1 (
    echo %date% %time% - aggregate.py FAILED - aborting before repair/purge >> daily_maintenance.log
    exit /b 1
)

.venv\Scripts\python.exe repair_mismatches.py --db tchoutchou.db --apply >> daily_maintenance.log 2>&1
if errorlevel 1 (
    echo %date% %time% - repair_mismatches.py FAILED - aborting before purge >> daily_maintenance.log
    exit /b 1
)

rem Non-zero exit here just means hard mismatches remain (e.g. the known station-87640912
rem cluster) -- logged for visibility, not treated as a pipeline failure.
.venv\Scripts\python.exe verify_aggregate.py --db tchoutchou.db >> daily_maintenance.log 2>&1

.venv\Scripts\python.exe purge_raw.py --db tchoutchou.db --retention-days 5 --vacuum >> daily_maintenance.log 2>&1

echo %date% %time% - daily maintenance complete >> daily_maintenance.log
