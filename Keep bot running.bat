@echo off
REM Turns ON auto-start: the ANDX bot will launch by itself every time you sign in.
REM Double-click to run. Double-click "Stop autostart.bat" to turn it off.
setlocal
set "BOTDIR=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LAUNCHER=%STARTUP%\ANDX Trading Bot.bat"

if not exist "%BOTDIR%.venv\Scripts\pythonw.exe" (
  echo The bot isn't set up yet. Run the install command once first, then try again.
  pause
  exit /b 1
)

> "%LAUNCHER%" echo @echo off
>> "%LAUNCHER%" echo cd /d "%BOTDIR%"
>> "%LAUNCHER%" echo start "" ".venv\Scripts\pythonw.exe" app.py

REM start it now too
cd /d "%BOTDIR%"
start "" ".venv\Scripts\pythonw.exe" app.py

echo.
echo Auto-start is ON.
echo Your bot will now start by itself every time you sign in to Windows,
echo as long as the computer is on and awake.
echo.
echo To turn this off later, double-click "Stop autostart.bat".
echo Open the dashboard anytime at http://127.0.0.1:8300
pause
