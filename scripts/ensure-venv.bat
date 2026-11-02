@echo off
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo The virtual environment points at a Python install that is no longer on this PC.
    echo Removing .venv and creating a new one...
    rmdir /s /q .venv
    if exist .venv (
      echo Could not remove .venv. Close any terminals using it, delete the folder, and run this again.
      exit /b 1
    )
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating a virtual environment with the current Python...
  py -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv. Install Python 3.12, then run this again.
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import django" >nul 2>&1
if errorlevel 1 (
  echo Installing Python dependencies...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 exit /b 1
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 exit /b 1
)

exit /b 0
