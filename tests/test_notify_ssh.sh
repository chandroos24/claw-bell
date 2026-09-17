#!/usr/bin/env bash
# Tests the SSH branch of notify-sound.sh: what it puts on the wire, and that
# it still falls back to the terminal bell when the listener is unreachable.
#
# Run: bash tests/test_notify_ssh.sh

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO/hooks/notify-sound.sh"
PASS=0
FAIL=0

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

ok()   { PASS=$((PASS+1)); printf '  ok      %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAILED  %s\n     %s\n' "$1" "$2"; }

check() { # name expected actual
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi
}

# A one-shot listener that writes the line it received to a file.
start_listener() {
    local outfile="$1"
    python3 -c '
import socket, sys
out = sys.argv[1]
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(1)
print(srv.getsockname()[1], flush=True)
srv.settimeout(10)
conn, _ = srv.accept()
data = conn.recv(256)
open(out, "wb").write(data)
conn.close()
' "$outfile" 2>/dev/null
}

# Runs the hook as if inside an SSH session on a host called $2, theme $3.
run_hook() { # event label theme port
    local event="$1" label="$2" theme="$3" port="$4"
    local home="$WORK/home-$label"
    mkdir -p "$home/.claude"
    printf '{"theme":"%s","chime_label":"%s","chime_port":%s}\n' \
        "$theme" "$label" "$port" > "$home/.claude/claw-bell.json"

    echo '{}' | env \
        HOME="$home" \
        CLAUDE_PLUGIN_ROOT="$REPO" \
        SSH_CONNECTION="10.0.0.9 5555 10.0.0.1 22" \
        TMUX_PANE= \
        bash "$HOOK" "$event"
}

echo "notify-sound.sh SSH branch"

# --- the happy path, once per host -----------------------------------------
for spec in "stop s1 dnd" "notification s2 classical"; do
    set -- $spec
    event="$1"; label="$2"; theme="$3"

    RECV="$WORK/recv-$label"
    exec 4< <(start_listener "$RECV")
    read -r PORT <&4

    run_hook "$event" "$label" "$theme" "$PORT" >/dev/null 2>&1
    rc=$?

    wait_for=0
    while [ ! -s "$RECV" ] && [ $wait_for -lt 50 ]; do sleep 0.1; wait_for=$((wait_for+1)); done
    exec 4<&-

    check "$label: exits 0"        "0"                              "$rc"
    check "$label: wire format"    "${event}|${theme}|${label}"     "$(cat "$RECV" 2>/dev/null | tr -d '\n')"
done

# --- no listener: must fall back to the bell, not hang or error -------------
RECV="$WORK/recv-none"
start=$(date +%s)
out=$(run_hook stop s1 dnd 9 2>/dev/null)
rc=$?
elapsed=$(( $(date +%s) - start ))

check "no listener: exits 0"      "0"       "$rc"
check "no listener: rings bell"   "1"       "$(printf '%s' "$out" | grep -c $'\a')"
if [ "$elapsed" -le 5 ]; then
    ok "no listener: returns promptly (${elapsed}s)"
else
    bad "no listener: returns promptly" "took ${elapsed}s"
fi

# --- a muted session must stay silent, tunnel or no tunnel ------------------
RECV="$WORK/recv-muted"
exec 4< <(start_listener "$RECV")
read -r PORT <&4
touch "$REPO/../.mute_all"
run_hook stop s1 dnd "$PORT" >/dev/null 2>&1
sleep 0.5
rm -f "$REPO/../.mute_all"
exec 4<&- 2>/dev/null
MUTED_BYTES=0
[ -s "$RECV" ] && MUTED_BYTES=$(wc -c < "$RECV" | tr -d ' ')
check "muted: sends nothing"      "0"       "$MUTED_BYTES"

echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
