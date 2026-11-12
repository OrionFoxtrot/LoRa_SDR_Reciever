@echo off
call "%~dp0receiver.bat" gui %*
exit /b %errorlevel%
