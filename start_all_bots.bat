@echo off
chcp 65001 >nul
echo Starting all bots...
echo.
echo Starting main bot in separate window...
start "Main Bot" cmd /k "cd /d %~dp0 && python elementBot.py || pause"
timeout /t 3 /nobreak >nul
echo.
echo Starting orders bot in separate window...
start "Orders Bot" cmd /k "cd /d %~dp0 && python orders_bot.py || pause"
timeout /t 3 /nobreak >nul
echo.
echo Starting purchases bot in separate window...
start "Purchases Bot" cmd /k "cd /d %~dp0 && python beats_purchases_bot.py || pause"
timeout /t 3 /nobreak >nul
echo.
echo All bots started! Check terminal windows.
echo If any window did not open or closed immediately, check for errors.
echo.
pause
