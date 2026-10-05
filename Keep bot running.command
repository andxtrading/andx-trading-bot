#!/bin/zsh
# Turns ON auto-start: the ANDX bot will launch by itself every time you log in.
# Double-click to run. Double-click "Stop autostart.command" to turn it off.
cd "$(dirname "$0")" || exit 1
BOTDIR="$(pwd)"
LA="$HOME/Library/LaunchAgents"
PLIST="$LA/com.andx.tradingbot.plist"

if [ ! -x "$BOTDIR/.venv/bin/python" ]; then
  echo "The bot isn't set up yet. Run the install command or 'Start Bot.command' once first, then try again."
  read "?Press Enter to close..."
  exit 1
fi

mkdir -p "$LA"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.andx.tradingbot</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BOTDIR/.venv/bin/python</string>
    <string>$BOTDIR/app.py</string>
  </array>
  <key>WorkingDirectory</key><string>$BOTDIR</string>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/andx-bot.log</string>
  <key>StandardErrorPath</key><string>/tmp/andx-bot.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"

echo ""
echo "Auto-start is ON."
echo "Your bot will now start by itself every time you log in to this Mac,"
echo "as long as the computer is on and awake."
echo ""
echo "To turn this off later, double-click 'Stop autostart.command'."
echo "Open the dashboard anytime at http://127.0.0.1:8300"
read "?Press Enter to close..."
