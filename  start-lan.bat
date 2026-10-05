@echo off
cd /d "%~dp0"
set STYPZY_NOPAUSE=1
title Stypzy Video server (network)
:loop
python server.py --lan
if errorlevel 2 goto setup
if errorlevel 1 goto crash
goto end
:crash
echo.
echo The server crashed. Details are in stypzy-error.log. Restarting in 3 seconds...
echo (close this window to quit)
timeout /t 3 >nul
goto loop
:setup
echo.
echo Setup problem - see the message above.
pause
:end