@echo off
REM Launch the lyrics overlay without a console window.
cd /d "%~dp0"
start "" pythonw -m app.main
