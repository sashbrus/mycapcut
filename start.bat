@echo off
title CupCut Studio
cd /d "%~dp0"

rem Use Anaconda Python (has fastapi/uvicorn/numpy); fall back to PATH python
set "PY=C:\Users\pointer\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

rem Listen on all interfaces so LAN (e.g. 10.100.102.x) can reach the API/UI
set "HOST=0.0.0.0"
set "PORT=8765"

echo Checking dependencies...
"%PY%" -m pip install -q -r requirements.txt

echo Starting CupCut Studio...
echo   local:  http://127.0.0.1:%PORT%
echo   LAN:    http://10.100.102.2:%PORT%  (HOST=%HOST%)
"%PY%" server.py

pause
