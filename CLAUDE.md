# claw-bell

Public repo. Never commit local usernames, hostnames, private IPs or personal
domains — not in code, docs, or commit messages.

## Running the real thing

- Claude Code runs the **installed copy** at
  `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, not this working
  tree. Repo edits change nothing until: bump version → merge to `main` →
  `claude plugin update` → restart Claude Code.
- Bump the version in 3 spots: `.claude-plugin/plugin.json` (once),
  `.claude-plugin/marketplace.json` (twice).

## Hooks

- `Stop` fires at the end of every turn. `Notification` only on
  `permission_prompt` / `elicitation_dialog`. The chime heard constantly is
  `Stop`.
- Listener wire format: `event|theme|label[|project]` — newline-terminated
  ASCII, 3 or 4 fields.
- The sender waits ≤2s for the listener's ack; anything added to the listener's
  request path must fit inside that.

## Config

- `config.json` in the plugin root, overlaid by `~/.claude/claw-bell.json`
  (survives plugin updates; per-host settings belong there).
- Defaults live in code, not in `config.json` — read the hook or listener for
  the real default.

## Tests

- No runner: `for t in tests/test_*.py; do python3 "$t"; done`, then
  `bash tests/test_*.sh`.
- Shell tests stub `afplay` and a fake listener, and point `HOME` at a temp dir
  to control config — the suite is silent by design. Keep it that way.

## macOS TTS

- `say -v '?'` costs ~0.9s — cache the listing, never call it on a hot path.
- Voice names contain spaces and parens (`Reed (English (US))`); only the full
  name disambiguates locale. Alex and Kate are absent from current macOS —
  never assume a voice exists.

## Style

- No inline Python heredocs in new shell code — add a subcommand to a `.py`
  helper and call it (see `hooks/project_speech.py`).
