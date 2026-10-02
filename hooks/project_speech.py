#!/usr/bin/env python3
"""Per-project chime speech, naming the project in both phrases:

    stop          "Task complete on <project>"
    notification  "Claude is waiting for you on <project>"


The pre-baked phrases under sounds/speech/ are cut once by Polly or
ElevenLabs and shipped with the plugin, so they cannot name a project nobody
had heard of at build time. This module fills that gap with the text-to-speech
voice the operating system already has — `say` on macOS, SAPI on Windows,
espeak on Linux — and caches the result under

    sounds/speech/projects/<accent>/<gender>/<event>_<slug>.wav

so only the first chime from a given checkout pays for synthesis. The cache
sits inside sounds/ on purpose: chime_web.py serves that tree, so the phone
gets the same phrase as the Mac rather than a generic beep.

Both ends of the remote path use this module. The hook turns a working
directory into a slug and puts it on the wire; the listener turns that slug
back into speech on the machine with the speakers. Only the slug crosses the
wire, so the listener never sees a remote path and never synthesizes anything
it did not first validate.

Used as a CLI by hooks/notify-sound.sh:

    project_speech.py slug < hook.json      -> bells-and-whistles
    project_speech.py wav --project X --event stop ...  -> stop_X.wav

The macOS voice is Samantha unless "project_voice" in config.json says
otherwise; a name macOS does not have falls back to the accent/gender list.
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# One per hook event. Both name the project, because which window to go to is
# the thing you cannot work out from the sound alone.
PHRASES = {
    "stop": "Task complete on {name}",
    "notification": "Claude is waiting for you on {name}",
}
DEFAULT_EVENT = "notification"

# Matches chime-listener.py's LABEL_RE, so a slug is always a legal wire field.
SLUG_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
MAX_SLUG = 48
SYNTH_TIMEOUT = 20

# The voice these phrases are spoken in, overridable with "project_voice" in
# config.json. Samantha is a current, natural macOS voice present on every
# recent release — unlike Alex, which Apple has dropped. It is deliberately
# independent of the "gender" setting, which selects among the shipped Polly
# phrases and says nothing about what the OS has installed.
DEFAULT_MAC_VOICE = "Samantha"

# Where to fall back when the chosen voice is not installed. First match wins;
# macOS ships a different set per release — Alex and Kate are gone from current
# versions, Fred is the 1980s robot — so each entry is a list, not a name.
MAC_VOICES = {
    ("us", "male"): ["Alex", "Tom", "Reed", "Rocko", "Eddy", "Fred"],
    ("us", "female"): ["Samantha", "Ava", "Allison", "Karen", "Flo", "Sandy"],
    ("uk", "male"): ["Daniel", "Oliver", "Reed", "Rocko", "Eddy"],
    ("uk", "female"): ["Kate", "Serena", "Fiona", "Shelley", "Flo", "Sandy"],
}

MAC_LOCALES = {"us": "en_US", "uk": "en_GB"}

# "Reed (English (US))   en_US   # Hello!" — the name may contain spaces, so
# the locale token is what separates name from the rest.
MAC_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<locale>[a-z]{2}_[A-Z]{2})\s")

ESPEAK_VOICES = {
    ("us", "male"): "en-us+m3",
    ("us", "female"): "en-us+f3",
    ("uk", "male"): "en-gb+m3",
    ("uk", "female"): "en-gb+f3",
}


def slugify(name):
    """A filename- and wire-safe slug, or None if nothing usable survives."""
    if not name:
        return None
    slug = SLUG_CHARS.sub("-", str(name)).strip("-._").lower()
    slug = re.sub(r"-{2,}", "-", slug)[:MAX_SLUG].strip("-._")
    return slug or None


def project_from_cwd(cwd):
    """The project name for a working directory: the directory's own name.

    Deliberately literal. Inferring a "better" name by walking up the tree
    guesses wrong more often than it helps, and the chime is only useful if
    the name it speaks is the one on the prompt.
    """
    if not cwd:
        return None
    return slugify(Path(cwd).expanduser().name)


def project_from_hook(payload, environ=None):
    """The project for one hook invocation: the payload's cwd, then the
    environment Claude Code sets, then wherever we happen to be."""
    environ = os.environ if environ is None else environ
    cwd = None
    if isinstance(payload, dict):
        cwd = payload.get("cwd")
    cwd = cwd or environ.get("CLAUDE_PROJECT_DIR") or environ.get("PWD")
    return project_from_cwd(cwd or os.getcwd())


def spoken_name(slug):
    """The slug as something a voice can read: separators become spaces."""
    return re.sub(r"[-._]+", " ", slug).strip()


def phrase_for(slug, event=DEFAULT_EVENT):
    template = PHRASES.get(event, PHRASES[DEFAULT_EVENT])
    return template.format(name=spoken_name(slug))


def voice_key(accent, gender, voice=None):
    """The cache bucket: whatever actually decides how the phrase sounds.

    Computed from the *requested* voice rather than the resolved one, so a
    cache hit costs a path lookup. Resolving a real voice means listing them,
    which takes most of a second — too much to pay on every chime.
    """
    if platform.system() == "Darwin":
        return slugify(voice or DEFAULT_MAC_VOICE) or f"{accent}-{gender}"
    return f"{accent}-{gender}"


def cache_path(sounds_dir, accent, gender, slug, event=DEFAULT_EVENT, voice=None):
    return Path(sounds_dir, "speech", "projects",
                voice_key(accent, gender, voice), f"{event}_{slug}.wav")


def ensure(sounds_dir, accent, gender, slug, event=DEFAULT_EVENT, voice=None,
           log=None):
    """The WAV naming this project for this event, cut on first use.

    Returns None rather than raising when there is no usable voice, so every
    caller degrades to the generic shipped phrase instead of going silent.
    """
    slug = slugify(slug)
    if not slug:
        return None
    if event not in PHRASES:
        event = DEFAULT_EVENT

    target = cache_path(sounds_dir, accent, gender, slug, event, voice)
    if target.is_file() and target.stat().st_size > 0:
        return target

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if log:
            log(f"project speech: cannot create {target.parent} ({exc})")
        return None

    if synthesize(phrase_for(slug, event), target, accent, gender, voice, log):
        return target
    return None


def synthesize(text, target, accent="us", gender="male", voice=None, log=None):
    """Render text to a WAV at target. True if the file is there afterwards.

    Writes to a temporary file in the same directory and renames, so a chime
    that fires mid-synthesis can never pick up a half-written WAV.
    """
    target = Path(target)
    try:
        handle, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=".{}.".format(target.stem), suffix=".wav"
        )
    except OSError as exc:
        if log:
            log(f"project speech: no scratch file for {target} ({exc})")
        return False
    os.close(handle)
    tmp = Path(tmp_name)

    try:
        command = _command_for(text, tmp, accent, gender, voice)
        if command is None:
            if log:
                log("project speech: no text-to-speech voice on this machine")
            return False
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=SYNTH_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if log:
                log(f"project speech: synthesis failed ({exc})")
            return False

        if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            if log:
                log(f"project speech: {command[0]} produced nothing for {text!r}")
            return False

        os.replace(str(tmp), str(target))
        if log:
            log(f"project speech: cached {target.name}")
        return True
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _command_for(text, target, accent, gender, voice=None):
    """The argv that renders text to target, or None if nothing can.

    `voice` names a macOS voice. Windows and Linux have their own voice
    namespaces, so there it is ignored and accent/gender still choose.
    """
    if platform.system() == "Darwin":
        return _mac_command(text, target, accent, gender, voice)
    if _is_wsl():
        return _wsl_command(text, target, gender)
    return _espeak_command(text, target, accent, gender)


def _mac_command(text, target, accent, gender, voice=None):
    command = ["say", "-o", str(target), "--data-format=LEI16@22050"]
    resolved = _mac_voice(accent, gender, voice)
    if resolved:
        command += ["-v", resolved]
    return command + ["--", text]


def installed_mac_voices():
    """Every installed voice as (full name, locale), in `say -v \'?\'` order."""
    found = []
    for line in _mac_voice_listing().splitlines():
        match = MAC_VOICE_LINE.match(line)
        if match:
            found.append((match.group("name").strip(), match.group("locale")))
    return found


def _match_mac_voice(wanted, accent, installed):
    """Resolve a requested name to one macOS actually has, or None.

    Returns it exactly as `say -v \'?\'` prints it, parentheses and all:
    several voices ship as both "Reed (English (US))" and "Reed (English
    (UK))", and only the full name picks the right one. A bare "Reed" matches
    either, so the requested accent's locale is preferred before any other.
    """
    if not wanted:
        return None
    target = wanted.strip().lower()
    locale = MAC_LOCALES.get(accent, "en_US")

    exact = [(full, loc) for full, loc in installed if full.lower() == target]
    base = [(full, loc) for full, loc in installed
            if full.split(" (")[0].lower() == target]
    for candidates in (exact, base):
        for full, loc in candidates:
            if loc == locale:
                return full
        if candidates:
            return candidates[0][0]
    return None


def _mac_voice(accent, gender, voice=None):
    """The voice to speak these phrases in.

    The configured name wins — that is the whole point of the setting. Only
    when it names something this Mac does not have do we fall back down the
    accent/gender list, because a voice nobody installed is not a reason to
    play nothing.
    """
    installed = installed_mac_voices()
    if not installed:
        return None

    resolved = _match_mac_voice(voice or DEFAULT_MAC_VOICE, accent, installed)
    if resolved:
        return resolved

    for name in MAC_VOICES.get((accent, gender), []):
        resolved = _match_mac_voice(name, accent, installed)
        if resolved:
            return resolved
    return None


def _mac_voice_listing(_cache=[]):
    """`say -v \'?\'` takes most of a second, and it cannot change mid-process."""
    if not _cache:
        try:
            _cache.append(subprocess.run(
                ["say", "-v", "?"],
                capture_output=True, text=True, timeout=10, check=False,
            ).stdout)
        except (OSError, subprocess.SubprocessError):
            _cache.append("")
    return _cache[0]


def _is_wsl():
    try:
        with open("/proc/version") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:
        return False


def _wsl_command(text, target, gender):
    powershell = _find_powershell()
    if not powershell:
        return None
    try:
        windows_path = subprocess.run(
            ["wslpath", "-w", str(target)],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None

    hint = "Male" if gender == "male" else "Female"
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"try {{ $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::{hint}) }} catch {{}}; "
        f"$s.SetOutputToWaveFile('{_ps_quote(windows_path)}'); "
        f"$s.Speak('{_ps_quote(text)}'); "
        "$s.Dispose()"
    )
    return [powershell, "-NoProfile", "-Command", script]


def _ps_quote(value):
    """Escape for a PowerShell single-quoted string."""
    return str(value).replace("'", "''")


def _find_powershell():
    from shutil import which

    found = which("powershell.exe")
    if found:
        return found
    for candidate in (
        "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
        "/mnt/c/Windows/SysWOW64/WindowsPowerShell/v1.0/powershell.exe",
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _espeak_command(text, target, accent, gender):
    from shutil import which

    for binary in ("espeak-ng", "espeak"):
        if which(binary):
            voice = ESPEAK_VOICES.get((accent, gender), "en-us")
            return [binary, "-v", voice, "-w", str(target), text]
    if which("pico2wave"):
        language = "en-GB" if accent == "uk" else "en-US"
        return ["pico2wave", "-l", language, "-w", str(target), text]
    return None


def prime():
    """Pay the one-off cost of enumerating voices up front.

    Only the listener calls this. It is a long-lived process answering chimes
    under a two-second ack deadline, and `say -v \'?\'` alone takes most of a
    second — long enough that the first unseen project could time the sender
    out and drop it to a terminal bell.
    """
    if platform.system() == "Darwin":
        _mac_voice_listing()


def main(argv=None):
    parser = argparse.ArgumentParser(description="per-project notification speech")
    sub = parser.add_subparsers(dest="mode", required=True)

    name = sub.add_parser("slug", help="print the project slug for a hook payload")
    name.add_argument("--cwd", default=None)

    wav = sub.add_parser("wav", help="print the cached WAV, synthesizing if needed")
    wav.add_argument("--project", required=True)
    wav.add_argument("--event", default=DEFAULT_EVENT, choices=sorted(PHRASES))
    wav.add_argument("--voice", default=None,
                     help=f"macOS voice name (default: {DEFAULT_MAC_VOICE})")
    wav.add_argument("--sounds-dir", required=True)
    wav.add_argument("--accent", default="us")
    wav.add_argument("--gender", default="male")

    args = parser.parse_args(argv)

    if args.mode == "slug":
        if args.cwd:
            slug = project_from_cwd(args.cwd)
        else:
            payload = {}
            if not sys.stdin.isatty():
                try:
                    payload = json.loads(sys.stdin.read() or "{}")
                except ValueError:
                    payload = {}
            slug = project_from_hook(payload)
        if slug:
            print(slug)
        return 0

    found = ensure(args.sounds_dir, args.accent, args.gender, args.project,
                   args.event, args.voice or None)
    if found:
        print(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
