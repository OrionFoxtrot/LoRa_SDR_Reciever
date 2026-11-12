@echo off
setlocal
set "FLEET_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%FLEET_PY%" set "FLEET_PY=python"
"%FLEET_PY%" -B "%~dp0fleet.py" %*
exit /b %errorlevel%
