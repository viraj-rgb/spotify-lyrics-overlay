@echo off
REM Same, but keeps the console so errors are visible.
cd /d "%~dp0"
python -m app.main
pause
