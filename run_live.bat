@echo off
call "%~dp0receiver.bat" live %*
exit /b %errorlevel%
