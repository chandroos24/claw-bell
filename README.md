# bells-and-whistles

A Claude Code plugin that plays notification sounds when your agent finishes
working or needs your attention. Because staring at a terminal, waiting, is no
way to live.

## What it does

Claude Code fires two events you care about: **Stop** (the agent finished) and
**Notification** (it wants permission or has a question). This plugin intercepts
both and plays a sound. Optionally, it speaks too — "Job completed!" or "Hey!
Back to work!" — so you can keep your eyes on the other monitor, guilt-free.

## Themes

Pick one. You will not be quizzed.

| Theme | Flavor |
|---|---|
| **Videogame** | Zelda secrets, Mario coins, MGS alerts, FF victory fanfares |
| **Disney** | "When You Wish Upon a Star," "Let It Go," and eight more you already know by heart |
| **Anime** | Evangelion brass, Sailor Moon transformations, Cowboy Bebop jazz stabs |
| **Movies** | Star Wars, Jaws, the Imperial March — the whole cinema lobby |
| **90s Rock** | Smoke on the Water, Enter Sandman, Seven Nation Army riffs in pure sine waves |
| **Classical** | Beethoven's Fifth, Fur Elise, Ride of the Valkyries |
| **Beeps** | Clean, professional tones for people who attend stand-ups with their camera on |
| **Chirps** | Robins, canaries, chickadees — an aviary in your terminal |
| **Cyberpunk 2077** | Neon drones, netrunner arpeggios, corpo alerts — Night City in sine waves |
| **D&D / Fantasy** | Tavern lutes, quest fanfares, spell casts — roll for initiative |

Each theme contains 10 short melodies. The plugin picks one at random, so
repetition stays tolerable.

## Voice

Voice announcements use **AWS Polly** (Stephen, Tiffany, Brian, or Amy).
If you run tmux, the voice tells you *which window* finished — useful when you
have six agents running and zero idea which one just spoke up.

Voice is optional. The plugin ships pre-generated WAV files and works fine
without AWS credentials.

### Themed phrases

The Cyberpunk and D&D themes include themed speech phrases — "Flatline
complete, choom!" instead of "Job completed!" Themed phrases are selected
randomly from a pool, so you get variety.

You can choose between themed and standard phrases during configuration. Edit
`speech_phrases.json` to customize or add your own phrases, then regenerate
with `generate_sounds.py`.

### ElevenLabs (alternative TTS)

You can use [ElevenLabs](https://elevenlabs.io) instead of AWS Polly to
generate speech with any voice from their library. ElevenLabs is a
generation-time alternative — at runtime the plugin just plays WAV files
regardless of which provider generated them.

To generate speech with ElevenLabs:

```bash
# Using --api-key flag
python3 generate_sounds.py --speech-only --tts-provider elevenlabs \
    --api-key YOUR_KEY --voice-id YOUR_VOICE_ID --accent us --gender male

# Or using environment variable
export ELEVENLABS_API_KEY=your_key
python3 generate_sounds.py --speech-only --tts-provider elevenlabs \
    --voice-id YOUR_VOICE_ID --accent us --gender male
```

The `--accent` and `--gender` flags determine which speech slot to overwrite
(for standard phrases) and which voice to select from the config mapping (if
`--voice-id` is not provided). For themed phrases, the output always goes to
`sounds/speech/themed/{theme}/`.

You can also configure a voice mapping in `config.json` so you don't need
`--voice-id` each time:

```json
{
  "elevenlabs_voices": {
    "us": {"male": "voice_id_1", "female": "voice_id_2"},
    "uk": {"male": "voice_id_3"}
  }
}
```

ElevenLabs requires `ffmpeg` for MP3→WAV conversion.

## Installation

Install from the Groundwork Marketplace. Add the marketplace once:

```
claude plugin marketplace add etr/groundwork-marketplace
```

Then install the plugin:

```
claude plugin install bells-and-whistles@groundwork-marketplace
```

Run the built-in setup command to choose your theme, mode, and voice:

```
/configure-bells-and-whistles
```

That writes a `config.json` in the plugin root and cleans up any old hook
configurations.

To update later:

```
claude plugin update bells-and-whistles@groundwork-marketplace
```

### Manual installation

If you prefer to clone the repo yourself:

```
claude plugins add /path/to/bells-and-whistles
```

## Muting

Silence notifications without changing your configuration:

```
/mute          # mute globally (or choose scope if in tmux)
/mute all      # mute all tabs
/mute tab      # mute just the current tmux window
```

```
/unmute        # unmute (auto-detects what's muted)
/unmute all    # unmute everything
/unmute tab    # unmute just the current tmux window
```

Per-tab mute requires tmux. Mute state persists until you unmute or delete the
marker files (`.mute_all`, `.mute_window_*`) from the plugin root.

## Configuration

Edit `config.json` directly if you prefer:

```json
{
  "mode": "sound_and_voice",
  "accent": "us",
  "gender": "male",
  "voice_style": "full_sentence",
  "theme": "cyberpunk",
  "use_themed_phrases": true
}
```

**mode** — `sound_and_voice`, `sound_only`, or `voice_only`

**accent** — `us` or `uk` (used for standard phrases; ignored when `use_themed_phrases` is true)

**gender** — `male` or `female` (used for standard phrases; ignored when `use_themed_phrases` is true)

**voice_style** — `full_sentence` or `number_only`

**theme** — one of: `videogame`, `disney`, `anime`, `movies`, `90s_rock`,
`classical`, `beeps`, `chirps`, `cyberpunk`, `dnd`

**use_themed_phrases** — `true` to use themed speech phrases (if available for
the selected theme), `false` to use standard "Job completed!" phrases

## Customizing phrases

Speech phrases live in `speech_phrases.json`. Each theme can define its own
phrases for `stop`, `notification`, `stop_window`, and `notification_window`
events. The `{window}` placeholder in window templates is replaced with the
tmux window number (0-9) at generation time.

After editing `speech_phrases.json`, regenerate speech files:

```bash
python3 generate_sounds.py --speech-only
```

## Platform support

| Platform | Playback method |
|---|---|
| WSL | `powershell.exe` via `System.Media.SoundPlayer` |
| macOS | `afplay` |
| Linux | `aplay`, falling back to `paplay` |
| SSH | Forwarded to a listener on your workstation, falling back to the terminal bell (`\a`) |

## Remote sessions over SSH

A Claude Code session on a headless server has no speakers. Rather than settle
for a terminal bell, the hook sends the *intent* back over the SSH connection
you already have open, and your workstation plays the sound. No audio crosses
the wire, and nothing listens on a public interface.

```
server: hook ──"stop|dnd|s1"──> 127.0.0.1:8127 ══tunnel══> workstation: listener ──> 🔊
```

See [docs/architecture.md](docs/architecture.md) for the full design.

**On the workstation** (macOS), install the plugin, then start the listener:

```bash
bash "$(claude plugin list --json | python3 -c 'import json,sys; print(next(p["installPath"] for p in json.load(sys.stdin) if p["id"].startswith("bells-and-whistles@")))')/hooks/install-listener.sh"
```

It installs a launchd agent that survives reboots. Re-run it after
`claude plugin update` — install paths are versioned, so the agent needs
repointing at the new one.

**In `~/.ssh/config`**, tunnel the port for each server:

```
Host myserver
    RemoteForward 8127 127.0.0.1:8127
```

Leave `ExitOnForwardFailure` at its default of `no`. A second concurrent
session to the same host cannot re-bind the port there; it should carry on and
reuse the first session's tunnel rather than refuse to connect.

**On each machine**, give it its own theme so you can tell who wants you
without looking. Put per-host settings in `~/.claude/claw-bell.json`, which is
layered over the plugin's `config.json` and survives plugin updates:

```json
{
  "theme": "dnd",
  "chime_label": "s1",
  "chime_port": 8127
}
```

Servers need nothing installed beyond the plugin itself — the send uses bash's
`/dev/tcp` builtin. If the tunnel is down, the listener is stopped, or the
workstation is asleep, the hook rings the terminal bell exactly as before.

Check `~/Library/Logs/claw-bell.log` on the workstation to see what arrived.

## Presence routing and the phone

The workstation can also serve the chime to a browser, so a phone on the same
private network makes the noise when you are not at your desk.

The listener reads whether you are actually at the Mac — logged in, screen
unlocked, and keyboard or mouse touched within `idle_threshold` — and uses it
twice: to decide whether to play locally, and as a `mac_active` flag on every
event the browser receives. Each device then applies its own rule.

| Phone toggle | At the Mac | Mac plays | Phone plays |
|---|---|---|---|
| Always | yes | yes | yes |
| Always | no | no | yes |
| When away from Mac | yes | yes | no |
| When away from Mac | no | no | yes |
| Off | yes | yes | no |
| Off | no | no | no |

Presence detection **fails open**: if it cannot read the idle time, the Mac
plays, which is the behaviour from before this feature existed.

Mute outranks all of it. `/mute` is enforced on the sending host, so a muted
session reaches neither device.

### Enabling it

Add to `~/.claude/claw-bell.json` on the workstation:

```json
{
  "web_enabled": true,
  "web_bind": ["127.0.0.1", "10.66.66.2"],
  "web_port": 8128,
  "presence_enabled": true,
  "idle_threshold": 300
}
```

| Key | Default | Meaning |
|---|---|---|
| `web_enabled` | `false` | serve the browser page at all |
| `web_bind` | `127.0.0.1` | address, or list of addresses, to bind — add your VPN address to reach the phone |
| `web_port` | `8128` | HTTP port |
| `presence_enabled` | `false` | gate local playback on whether you are at the Mac |
| `idle_threshold` | `300` | seconds of inactivity before you count as away |

Then re-run `install-listener.sh`; it prints the URL when the web server is on.

**`web_bind` is never `0.0.0.0`.** Bind only the interfaces you want reachable.
With a WireGuard or Tailscale address, VPN membership is the only
authentication — there is no login, and the page reveals which hosts chimed and
when.

Give it a list including `127.0.0.1`. On macOS a WireGuard `utun` is
point-to-point, so packets the Mac sends to its own tunnel address are routed
*into* the tunnel rather than looped back — bind the VPN address alone and the
Mac cannot open its own page, even though every peer can. An address that
fails to bind is logged and skipped, so a VPN that is down does not take the
local page with it.

### On the phone

Open the URL, tap **Tap to enable sound** once — browsers block audio until a
gesture — then pick a toggle. The page shows a live list and a status dot:
green when a heartbeat arrived recently, amber when one is missed, red when
disconnected. The amber state matters: most mobile failures look like a working
page that silently delivers nothing.

Audio only plays while the tab is open and in the foreground. On Android, the
optional **keep playing with the screen off** checkbox starts a silent looping
track plus a MediaSession, which keeps the tab alive with the screen off — at
the cost of a persistent media notification and battery. iOS ignores it.

On Samsung One UI, set both Chrome and your VPN app to **Unrestricted** battery
usage, or "sleeping apps" will evict them and the page will go quiet.

## Regenerating sounds

The pre-generated WAV files live in `sounds/`. To regenerate melodies (no
dependencies beyond Python 3):

```
python3 generate_sounds.py --melodies-only
```

To regenerate speech files (requires AWS CLI with Polly access):

```
python3 generate_sounds.py --speech-only
```

You can narrow the scope with `--theme`, `--accent`, and `--gender`.

## License

MIT — see [LICENSE](LICENSE).

## Special Thanks
- https://github.com/jackmarketon for Elevenlabs integration, D&D/Cyberpunk audio, and configurable phrases.
