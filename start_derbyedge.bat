@echo off
rem DerbyEdge launcher: double-click, or pin a shortcut to the desktop.
rem Starts the operator console from the folder this file lives in. Close the window (or Ctrl+C) to stop it.
cd /d "%~dp0"
title DerbyEdge Engine

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
    echo Using .venv
) else (
    echo No .venv folder found, using the Python on your PATH
)

python --version >nul 2>&1
if errorlevel 1 (
    echo Python was not found. Install Python 3.12 or create .venv, then try again.
    pause
    exit /b 1
)

echo Starting DerbyEdge at http://127.0.0.1:8501 ...
start "" "http://127.0.0.1:8501"
python -m streamlit run src/app/app.py --server.address 127.0.0.1 --server.port 8501

echo.
echo DerbyEdge stopped. If this was not expected, the message above says why.
pause
