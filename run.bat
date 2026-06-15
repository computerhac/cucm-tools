@echo off
:: CUCM Tools launcher for Windows
:: Double-click this file to set up and start the app.

setlocal

set VENV_DIR=%~dp0venv

:: Create virtual environment if it doesn't exist
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Setting up virtual environment for the first time...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Python not found. Please install Python 3.11+ from https://www.python.org/downloads/
        echo Make sure to check "Add Python to PATH" during installation.
        pause
        exit /b 1
    )
)

:: Activate venv
call "%VENV_DIR%\Scripts\activate.bat"

:: Install / update dependencies
echo Checking dependencies...
pip install -q -r "%~dp0requirements.txt"
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

cd /d "%~dp0"
python launch.py

pause
