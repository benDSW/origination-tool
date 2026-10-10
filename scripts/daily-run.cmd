@echo off
rem Daily origination run for a local install (used by Windows Task Scheduler).
rem Output is appended to logs\daily.log. Same job Railway's cron service runs.
cd /d "%~dp0.."
if not exist logs mkdir logs
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" set "UV=uv"
echo ==== %DATE% %TIME% ==== >> logs\daily.log
"%UV%" run python -m alembic upgrade head >> logs\daily.log 2>&1
"%UV%" run python -m app.jobs.daily >> logs\daily.log 2>&1
