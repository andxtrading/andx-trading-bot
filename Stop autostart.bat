@echo off
REM Turns OFF auto-start AND stops the bot that is running now.
REM Double-click to run.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ANDX Trading Bot.bat" 2>nul
taskkill /F /IM pythonw.exe 2>nul
taskkill /F /IM python.exe 2>nul
echo.
echo Auto-start is OFF and the bot is stopped.
echo It will not start on its own anymore.
echo.
echo To run it again, double-click "Start Bot (Windows).bat" or paste the install command.
echo To make it auto-start again, double-click "Keep bot running.bat".
pause
