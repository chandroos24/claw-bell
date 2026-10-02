#!/usr/bin/env bash
# Tests the local playback branch of notify-sound.sh: which WAVs it hands to
# the player when a session is on the machine with the speakers.
#
# A stub stands in for afplay, so the suite is silent and asserts on the exact
# files chosen rather than on anything audible.
#
# Run: bash tests/test_notify_local.sh

set -u

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO/hooks/notify-sound.sh"
PASS=0
FAIL=0

if [ "$(uname)" != "Darwin" ]; then
    echo "notify-sound.sh local branch: skipped (stubs afplay; macOS only)"
    exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/bin" "$WORK/home/.claude"
cat > "$WORK/bin/afplay" <<'STUB'
#!/bin/bash
printf '%s\n' "$1" >> "$AFPLAY_LOG"
STUB
chmod +x "$WORK/bin/afplay"

ok()  { PASS=$((PASS+1)); printf '  ok      %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAILED  %s\n     %s\n' "$1" "$2"; }

check() { # name expected actual
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected '$2', got '$3'"; fi
}

contains() { # name needle haystack
    case "$3" in
        *"$2"*) ok "$1" ;;
        *) bad "$1" "'$2' not among: $(printf '%s' "$3" | tr '\n' ' ')" ;;
    esac
}

absent() { # name needle haystack
    case "$3" in
        *"$2"*) bad "$1" "'$2' should not have played" ;;
        *) ok "$1" ;;
    esac
}

# Runs the hook as a local session in $2, with config $3, and echoes the
# basenames of every WAV the stub player was handed.
run_hook() { # event cwd config_json
    local event="$1" cwd="$2" config="$3"
    printf '%s' "$config" > "$WORK/home/.claude/claw-bell.json"
    : > "$WORK/log"
    printf '{"cwd":"%s"}' "$cwd" | env \
        PATH="$WORK/bin:$PATH" \
        HOME="$WORK/home" \
        CLAUDE_PLUGIN_ROOT="$REPO" \
        AFPLAY_LOG="$WORK/log" \
        SSH_CONNECTION= \
        TMUX_PANE= \
        bash "$HOOK" "$event"
    # Playback is backgrounded; wait for it rather than guessing at a sleep.
    local waited=0
    while [ ! -s "$WORK/log" ] && [ $waited -lt 100 ]; do
        sleep 0.1
        waited=$((waited+1))
    done
    sleep 0.2
    sed 's|.*/||' "$WORK/log"
}

VOICE='{"theme":"dnd","mode":"voice_only","gender":"male","accent":"us"}'

echo "notify-sound.sh local branch"

rm -rf "$REPO/sounds/speech/projects"

# --- the headline: both events name the project, each in its own words ------
played=$(run_hook notification /srv/bells-and-whistles "$VOICE")
contains "a blocked turn speaks the project" \
    "notification_bells-and-whistles.wav" "$played"

played=$(run_hook stop /srv/bells-and-whistles "$VOICE")
contains "a finished turn speaks the project" \
    "stop_bells-and-whistles.wav" "$played"
absent "and does not reuse the waiting phrase" \
    "notification_bells-and-whistles" "$played"

# --- and it is cut once, not once per chime ---------------------------------
CACHED="$REPO/sounds/speech/projects/us/male/stop_bells-and-whistles.wav"
if [ -f "$CACHED" ]; then
    before=$(stat -f %m "$CACHED")
    run_hook stop /srv/bells-and-whistles "$VOICE" >/dev/null
    after=$(stat -f %m "$CACHED")
    check "the phrase is cached, not recut" "$before" "$after"
else
    bad "the phrase is cached, not recut" "no WAV at $CACHED"
fi

# --- the melody still plays alongside it ------------------------------------
BOTH='{"theme":"dnd","mode":"sound_and_voice","gender":"male","accent":"us"}'
played=$(run_hook notification /srv/bells-and-whistles "$BOTH")
check "melody and project phrase, in that order" "2" "$(printf '%s\n' "$played" | grep -c .)"
contains "melody comes from the theme" ".wav" "$(printf '%s' "$played" | head -1)"
contains "project phrase follows it" \
    "notification_bells-and-whistles.wav" "$(printf '%s' "$played" | tail -1)"

# --- opting out -------------------------------------------------------------
OPTOUT='{"theme":"dnd","mode":"voice_only","gender":"male","project_announce":false}'
played=$(run_hook stop /srv/bells-and-whistles "$OPTOUT")
absent "project_announce false opts out" "stop_bells-and-whistles" "$played"
contains "and falls back to the shipped phrase" "stop_0" "$played"

# --- a directory with nothing usable in its name ----------------------------
played=$(run_hook stop / "$VOICE")
contains "no usable project name falls back" "stop_0" "$played"

# --- sound_only never speaks at all -----------------------------------------
SILENT='{"theme":"dnd","mode":"sound_only","gender":"male"}'
played=$(run_hook stop /srv/bells-and-whistles "$SILENT")
check "sound_only plays one melody and no speech" "1" "$(printf '%s\n' "$played" | grep -c .)"
absent "sound_only says nothing" "stop_" "$played"

rm -rf "$REPO/sounds/speech/projects"

echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
