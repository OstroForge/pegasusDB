@echo off
setlocal
cd /d "%~dp0"

call "%~dp0scripts\ensure-venv.bat"
if errorlevel 1 exit /b 1
call .\.venv\Scripts\activate.bat
REM Bytecode stays outside OneDrive. Cloud-only .pyc files hang Python on import.
set PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\pegasusDB-pyc

python manage.py global_test %*

endlocal