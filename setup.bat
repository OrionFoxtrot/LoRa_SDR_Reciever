@echo off
setlocal
pushd "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" goto check
where python >nul 2>&1
if errorlevel 1 goto usepy
python -m venv --system-site-packages .venv
if errorlevel 1 exit /b 1
goto check
:usepy
py -3 -m venv --system-site-packages .venv
if errorlevel 1 exit /b 1
:check
".venv\Scripts\python.exe" -B fleet.py doctor
if not errorlevel 1 exit /b 0
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -B fleet.py doctor
exit /b %errorlevel%
