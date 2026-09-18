#!/usr/bin/env bash
# Install (or remove) the claw-bell chime listener as a launchd agent.
#
#   bash hooks/install-listener.sh              install and start
#   bash hooks/install-listener.sh --uninstall  stop and remove
#
# Run this once on the workstation that should make the noise, and again after
# `claude plugin update` — the plugin's install path is versioned, so the agent
# needs to be repointed at the new one.
#
# Only the workstation needs this. Remote hosts just send a line over the
# ssh RemoteForward tunnel; see docs/architecture.md.

set -euo pipefail

LABEL="com.chandroos.claw-bell"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/claw-bell.log"

if [ "$(uname)" != "Darwin" ]; then
    echo "This installer is macOS-only (it uses launchd and afplay)." >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL"
    exit 0
fi

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LISTENER="$PLUGIN_ROOT/hooks/chime-listener.py"
PYTHON="$(command -v python3)"

[ -f "$LISTENER" ] || { echo "Listener not found at $LISTENER" >&2; exit 1; }
[ -n "$PYTHON" ]   || { echo "python3 not found on PATH" >&2; exit 1; }

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$LISTENER</string>
        <string>--log-file</string>
        <string>$LOG</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

# Give it a moment, then confirm it is actually accepting connections.
sleep 1
PORT=$(python3 -c "
import json, os
cfg = {}
for p in ('$PLUGIN_ROOT/config.json', os.path.expanduser('~/.claude/claw-bell.json')):
    try:
        d = json.load(open(p))
        if isinstance(d, dict):
            cfg.update(d)
    except Exception:
        pass
print(cfg.get('chime_port', 8127))
")

# Ping rather than a bare port probe: it confirms the listener is actually
# answering, not merely that something holds the port, and it stays silent.
# Retried, because launchd start plus module import is not instant and a single
# early probe reports a working install as broken.
PONG=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
    PONG=$( (exec 3<>"/dev/tcp/127.0.0.1/$PORT" 2>/dev/null \
             && printf 'ping\n' >&3 \
             && { read -t 3 -r reply <&3; echo "$reply"; } \
             && exec 3>&-) 2>/dev/null )
    [ "$PONG" = "pong" ] && break
    sleep 1
done

if [ "$PONG" = "pong" ]; then
    echo "claw-bell listener running on 127.0.0.1:$PORT"
    echo "  plist: $PLIST"
    echo "  log:   $LOG"
    WEB=$(python3 -c "
import json, os
cfg = {}
for p in ('$PLUGIN_ROOT/config.json', os.path.expanduser('~/.claude/claw-bell.json')):
    try:
        d = json.load(open(p))
        if isinstance(d, dict):
            cfg.update(d)
    except Exception:
        pass
if str(cfg.get('web_enabled', False)).lower() == 'true':
    print('http://%s:%s' % (cfg.get('web_bind', '127.0.0.1'), cfg.get('web_port', 8128)))
")
    [ -n "$WEB" ] && echo "  web:   $WEB"
else
    echo "Agent loaded but nothing is listening on port $PORT." >&2
    echo "Check $LOG for the reason." >&2
    exit 1
fi
