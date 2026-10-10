@echo off
rem Local dashboard: applies any new migrations, then serves on http://127.0.0.1:8000
rem (bound to this computer only). Close this window to stop it.
cd /d "%~dp0.."
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" set "UV=uv"
"%UV%" run python -m alembic upgrade head || goto :error
start "" http://127.0.0.1:8000
"%UV%" run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof

:error
echo Database migration failed - see the messages above.
pause
