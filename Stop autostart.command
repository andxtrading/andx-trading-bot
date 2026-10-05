#!/bin/zsh
# Turns OFF auto-start AND stops the bot that is running now.
# Double-click to run.
PLIST="$HOME/Library/LaunchAgents/com.andx.tradingbot.plist"

launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"
pkill -f "/.venv/bin/python app.py" 2>/dev/null
pkill -f "python app.py" 2>/dev/null

echo ""
echo "Auto-start is OFF and the bot is stopped."
echo "It will not start on its own anymore."
echo ""
echo "To run it again, double-click 'Start Bot.command'."
echo "To make it auto-start again, double-click 'Keep bot running.command'."
read "?Press Enter to close..."
