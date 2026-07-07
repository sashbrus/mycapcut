@echo off
title CupCut Studio
cd /d "%~dp0"

rem Use Anaconda Python (has fastapi/uvicorn/numpy); fall back to PATH python
set "PY=C:\Users\pointer\anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

echo Checking dependencies...
"%PY%" -m pip install -q -r requirements.txt

echo Starting CupCut Studio at http://127.0.0.1:8765 ...
"%PY%" server.py

pause
