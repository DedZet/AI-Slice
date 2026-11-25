@echo off
cd /d "%~dp0backend"
set "AISLICE_PYTHON=python"
if exist "..\venv\Scripts\python.exe" set "AISLICE_PYTHON=..\venv\Scripts\python.exe"
if exist "..\.venv\Scripts\python.exe" set "AISLICE_PYTHON=..\.venv\Scripts\python.exe"
"%AISLICE_PYTHON%" -B -m uvicorn main:app --host 127.0.0.1 --port 8000 --loop asyncio
