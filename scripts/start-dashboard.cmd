@echo off
rem Local dashboard: applies any new migrations, then serves on http://127.0.0.1:8000
rem (bound to this computer only). Close this window to stop it.
title DSW Origination Dashboard
cd /d "%~dp0.."
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" set "UV=uv"

findstr /b /c:"ADMIN_PASSWORD_HASH=$argon2" .env >nul 2>&1
if errorlevel 1 (
    echo WARNING: ADMIN_PASSWORD_HASH is not set in .env - logins will fail.
    echo Run:  uv run python -m app.jobs.hash_password   and add the printed line to .env
    echo.
)

echo Updating database...
"%UV%" run python -m alembic upgrade head
if errorlevel 1 goto failed

echo Starting dashboard - your browser will open in a few seconds.
rem Open the browser only once the server has had time to start.
start "" /min cmd /c "timeout /t 8 /nobreak >nul & start "" http://127.0.0.1:8000"
"%UV%" run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo The dashboard has stopped.
pause
exit /b

:failed
echo.
echo Database migration failed - see the messages above.
pause
