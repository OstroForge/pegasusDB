@echo off
setlocal
cd /d "%~dp0"

call "%~dp0scripts\ensure-venv.bat"
if errorlevel 1 exit /b 1
call .\.venv\Scripts\activate.bat
REM Bytecode stays outside OneDrive. Cloud-only .pyc files hang Python on import.
set PYTHONPYCACHEPREFIX=%LOCALAPPDATA%\pegasusDB-pyc

python -m pip install --upgrade pip
pip install -r requirements.txt

python manage.py makemigrations unicorn_project.training
python manage.py migrate

REM Load fixtures only if empty
python -c "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','unicorn_project.settings'); import django; django.setup(); from unicorn_project.training.models import Booking; from django.core.management import call_command; print('Checking fixtures...'); call_command('loaddata', 'unicorn_project/training/fixtures.json') if not Booking.objects.exists() else None"

echo.
echo Launching server at http://127.0.0.1:8000/
python manage.py runserver

endlocal
