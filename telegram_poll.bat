@echo off
setlocal
cd /d "%~dp0"

call "%~dp0scripts\ensure-venv.bat"
if errorlevel 1 exit /b 1
REM Bytecode stays outside OneDrive. Cloud-only .pyc files hang Python on import.
set PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\pegasusDB-pyc

rem Stop orphan pollers left behind by Ctrl+C (Windows often keeps python.exe running)
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" | Where-Object { $_.CommandLine -match 'manage.py telegram_poll' -and $_.CommandLine -match 'pegasusDB' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

ping 127.0.0.1 -n 3 >nul

.\.venv\Scripts\python.exe manage.py telegram_poll

endlocal
