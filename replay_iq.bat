@echo off
if "%~1"=="" goto pick
call "%~dp0receiver.bat" replay %*
exit /b %errorlevel%
:pick
call "%~dp0receiver.bat" gui --replay
exit /b %errorlevel%
