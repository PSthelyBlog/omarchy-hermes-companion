# Hermes Companion (Omarchy plugin)

![Hermes Companion panel on the Omarchy desktop](preview.jpg)

Always-on Hermes agent: watches the focused monitor, speaks up when it judges it useful,
answers voice requests on demand (right-click the bar icon, the Listen button, or Super+Alt+H). Models: a **vision** model (must accept images; sees the screen) and an optional separate **reasoning** model
(default = same as vision). Lists are built from every provider Hermes has credentials for, grouped by provider,
with vision capability from models.dev / the Nous catalog. When the two differ, the vision model describes each
frame in a stateless one-shot and the reasoning model runs the persistent conversation on that text.
Only your requests use tools: read-only ones (web search/extract, file read/search) unless you turn on **Actions**
(below). Screen ticks get none (see *Privacy*). It answers in the language you use and can be told who you are
(`user_context`) — see *Configuration*.

## Requirements
- Omarchy 4.x (`omarchy`, `omarchy-shell`, `grim`, `hyprctl`, `notify-send`, PipeWire tools) — `install.sh` offers `omarchy pkg add` for missing packages.
- Hermes Agent ≥ 0.21.7 at `~/.hermes/hermes-agent` (or `HERMES_AGENT_DIR`) — `install.sh` offers `omarchy install ai hermes` / `omarchy install hermes cli` if absent.
  Omarchy's Hermes package installs an older release first: run `hermes update` once before installing the companion.
- At least one model provider credential (`hermes auth add …` or a Claude Code login). Not required to install; pick a vision model in the widget afterwards.
- Local speech-to-text (`faster-whisper`, from Hermes's `voice` extra, which `install.sh` asks Hermes to install) for voice requests.

## Install
```bash
omarchy plugin add https://github.com/PSthelyBlog/omarchy-hermes-companion.git --enable
```
Enabling the plugin opens the installer in a floating terminal the first time (checks Omarchy tooling and Hermes,
has Hermes install its `voice` extra, writes the systemd --user unit and Hyprland keybinds, starts the daemon).
Re-run it anytime: `~/.config/omarchy/plugins/hermes.companion/install.sh`. Update with `omarchy plugin update hermes.companion`,
then `omarchy restart shell`: the shell keeps the plugin's always-loaded service (toasts, approval cards) from when it
started, so a new version of it only loads after a shell restart or a new login.

## Uninstall
```bash
~/.config/omarchy/plugins/hermes.companion/uninstall.sh   # stops/disables the daemon, removes unit + keybinds
omarchy plugin remove hermes.companion                    # removes the plugin checkout
```
Hermes's `voice` extra is left installed, since Hermes's own voice mode uses it too; remove it with
`hermes pm install --without voice` if unwanted. Runtime state lives in `~/.local/state/hermes-companion/`.

## External dependencies
| Dependency | Why | Installed by |
|---|---|---|
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) ≥ 0.21.7 (MIT) | model calls, STT/TTS pipeline, provider credentials | user / `omarchy install ai hermes`, then `hermes update` |
| `faster-whisper`, `sounddevice` (PyPI) | local speech-to-text, mic capture | Hermes's `voice` extra: `install.sh` runs `hermes pm install --extra voice` |
| `pillow` (PyPI) | screenshot scaling | Hermes (core dependency) |
| Python 3 (`/usr/bin/python3`) | starts the daemon and `--ctl` commands | Omarchy base |
| `grim`, `hyprctl`, `notify-send`, PipeWire (`pw-record`, `pactl`, `wpctl`) | screenshots, window info, notifications, audio | Omarchy base; `install.sh` offers `omarchy pkg add` if missing |
| Edge TTS (via Hermes, network) | default voice output | Hermes |
| Model provider APIs (Anthropic, Nous Portal, …) | screen ticks and answers are sent to the selected provider | user credentials via `hermes auth` |

Screenshots are held in memory only and sent to the selected vision model; nothing is written to disk by the plugin
besides its state/config files. No `sudo` or `pkexec` is required.

### Python dependencies
The plugin installs no Python packages itself. Since 0.21.7, Hermes's package manager builds the environment
Hermes runs from (under `~/.hermes/installs/`) from Hermes's own locked `uv.lock`, and rebuilds it on updates.
Anything installed beside it would vanish at the next update, and a separate lock could only ever match one
Hermes release. So `install.sh` asks Hermes for its `voice` extra (`hermes pm install --extra voice`): Hermes
installs `faster-whisper` and `sounddevice` at the versions it ships with the rest of its packages, and keeps them
through every `hermes update`.

The daemon starts on `/usr/bin/python3` and imports Hermes's bootstrap before any third-party package. The
bootstrap re-executes it on the Python Hermes manages and leases the environment Hermes committed, the same way
Hermes starts its own services, so an update never removes that environment while the daemon runs. `--ctl`
commands use only the standard library and stay on the system Python.

## Actions (off by default)

Toggle **Actions** in the popup (or `--ctl toggle-actions`). Voice/text requests can then get
things *done*: the companion never runs commands itself — it hands the task to a Hermes
subagent (`delegate_task`) that has `terminal` and file tools. Screen ticks can never trigger
actions. Each hand-over asks you first: its card shows the task and its context, the question is
spoken, and nothing starts without your yes. A task can't carry images.
Every child command then passes through Hermes' own safety gate plus the companion policy
(`daemon/actions.py`):

| Tier | What | Behaviour |
|---|---|---|
| 0-3 | reads, builds, git, writes/deletes inside `$HOME` (not dotfiles), `/tmp` | runs |
| 4 | Hermes dangerous patterns (recursive delete, force push, pipe-to-shell, service restarts, …), anything touching dotfiles or paths outside `$HOME`, `pkill`, credential files | **asks**: toast with Run / Skip + spoken "May I run …? say yes or no" (30 s, silence = no) |
| 5 | Hermes hardline floors (`rm -rf ~`, `dd` to devices, `mkfs`, …), `sudo`/`pkexec`, system-scope `systemctl`, shutdown | refused |

In the companion, Hermes's approvals run in `manual` mode whatever `approvals.mode` says, so every approval comes to
you: never to the smart-mode guardian model, and `off` doesn't skip it. A command that Hermes's own gate flags is asked
there (on the same card), which also lets through commands you have told Hermes to always allow. Only yolo would skip
the asking; the daemon never turns it on, and the companion refuses such calls if it is.

Each approval request has an id, shown as `approval` in `--ctl status` while it waits. Run / Skip and
`--ctl decide <request> <yes|no>` answer only that request: once it is settled (answered, by voice too, or
timed out) its card withdraws, and a late answer to it is refused instead of approving the next command.

Audit log: `~/.local/state/hermes-companion/actions.jsonl` (every run, write, approval, refusal).

## Privacy
Frames are skipped while any window on the captured monitor (not just the focused one) is a password manager,
private browsing, or a banking/OTP window, while a notification popup, the clipboard history, an authentication
prompt or the Wi-Fi QR code is showing, and on the lock screen — or whenever the lock state can't be read (see
`daemon/perception.py`); you can pause the eyes anytime from the widget or `Super+Alt+E`. While the screen is shared
or recorded, unprompted observations and remarks are not drawn as toasts (held remarks go to the panel's recent
list); replies to your own requests still are.

Screen ticks run no tools. A tick is driven by what is on screen, and a webpage, a message or a window title can put
text there, so every tool call made during a tick is refused before it runs, with Actions on or off. The screen stays
in the conversation, so requests don't trust it either: once the conversation has read a local file (or a helper has
run a command), a web search or page fetch first shows its query or URLs on an approval card and asks out loud;
silence = no. **Forget** resets this.

Your data goes only to the models you picked for the companion. Requests and helpers run a fixed set of tools (web
search and fetch, file read, search and write, and with Actions the terminal and the helper itself); anything else is
refused. There are no image tools: the only images a model gets are the screen frames, sent to your vision model.
Hermes would send an image, and the question about it, to its auxiliary vision model, which can belong to another
provider. Hermes's context compression is off for the same reason, in the companion and its helpers, since it
summarizes the conversation with an auxiliary model; the companion keeps the conversation short itself instead
(below). A helper runs on the companion's model; if Hermes's `delegation` settings pin another provider, model or
endpoint, hand-overs are refused.

Secret values the models write out (API keys and tokens in well-known formats, private keys, `NAME=value` lines whose
name ends in key/token/password…) are masked before they are toasted, stored, logged or spoken, and in split mode
before the vision model's description reaches the reasoning model. Screenshots themselves are not altered.
The conversation (and with it the frames, descriptions and window titles seen) is kept in memory until the daemon
restarts or you press **Forget** in the panel (`--ctl forget`); pausing the eyes stops new captures but does not
forget. It is trimmed as it grows: past about 120 screen checks or 60k tokens, the oldest screen checks go first,
then your oldest requests. Switching to a model from a different provider carries only your spoken/typed exchanges over, never the
screen history.

## Layout
- `daemon/companion.py`  main loop · `--ctl <cmd>` talks to the running daemon
- `daemon/perception.py` grim + hyprctl, dHash change detection, privacy filter (RAM only)
- `daemon/brain.py`      persistent Hermes AIAgent (web/file read-only), JSON tick protocol; native vs split vision
- `daemon/catalog.py`    provider/model catalog with vision flags (Hermes credentials + models.dev + Nous catalog)
- `daemon/voice.py`      on-demand Hermes VAD capture → local faster-whisper → edge TTS
- `daemon/actions.py`    tool policy (no tools on screen ticks, asks before local data leaves, helper tasks) and the tiered approval policy for the helper's commands (see *Actions*, *Privacy*)
- `daemon/prompt.md`     system prompt; `{{USER}}`, `{{USER_CONTEXT}}`, `{{LANGUAGE_RULE}}` are filled from `companion.json`
- `daemon/policy.py`     cooldowns / fullscreen / call / idle gating for unprompted speech
- `daemon/state.py`      `~/.local/state/hermes-companion/state.json` + `$XDG_RUNTIME_DIR/hermes-companion.sock`
- `BarWidget.qml`        bar eye icon + panel (toggles, model pickers, text input, recent replies)   `Service.qml` starts the unit, renders toasts
- `companion.json`       see *Configuration*
- `hermes-companion.service.in` template → `~/.config/systemd/user/hermes-companion.service` (install.sh fills in the Hermes and plugin paths)

## Configuration
`~/.config/omarchy/plugins/hermes.companion/companion.json` (start from `companion.example.json`; the panel and `--ctl set-*` write to it too).

| Key | Default | Meaning |
|---|---|---|
| `user_name` | `$USER` capitalised | how the companion addresses you |
| `user_context` | `""` | free text about who you are / how you work, injected into the system prompt (empty = "is a developer") |
| `language` | `"auto"` | reply language: `auto` follows whatever language you speak/type; a name (`"German"`) pins it |
| `eyes` | `true` | screen-watching on at daemon start (`toggle-eyes` / Super+Alt+E changes it at runtime only) |
| `actions` | `false` | let requests delegate shell/file work to a helper subagent (see *Actions*) |
| `notify` | `true` | also send spoken remarks as desktop notifications |
| `toast_position` | `{"anchor": "bottom-right", "margin_x": 0, "margin_y": 0}` | corner for toasts (`top-right`, `top-left`, `bottom-right`, `bottom-left`) plus extra pixels from that corner; applies without restart |
| `vision` / `reasoning` | `{model, effort, thinking}` | `provider:model`, `low|medium|high`, extended thinking on/off; empty reasoning model = same as vision |
| `tick_seconds` | `25` | screen check interval |
| `change_threshold` | `12` | dHash distance below which a frame counts as unchanged; unchanged screens are only re-checked every ~2 min |
| `max_width` | `1280` | frames are downscaled to this width before upload |
| `idle_skip_seconds` | `300` | no ticks after this much input idleness |
| `min_gap_seconds` / `urgent_gap_seconds` / `max_per_hour` | `300` / `60` / `8` | rate limits for unprompted speech |
| `hermes_dir` | `~/.hermes/hermes-agent` | Hermes checkout (written by `install.sh`) |

## Commands
```
CTL="/usr/bin/python3 $HOME/.config/omarchy/plugins/hermes.companion/daemon/companion.py --ctl"
$CTL status | toggle-eyes | listen | toggle-mute | toggle-toasts | toggle-actions | decide <request> <yes|no> | hush | tick | forget | models | set-vision <provider:model> | set-reasoning <provider:model|same> | set-{vision,reasoning}-effort <low|medium|high> | toggle-{vision,reasoning}-thinking | set-language <auto|language name> | set-user-context <text|(empty)> | set-toast-position <anchor>[,<margin_x>[,<margin_y>]] | say <text> | ask <text> | toast <text> | quit
# ask / text: same as speaking to it (reply is toasted + spoken); text is what the panel's input box sends
# forget: drop the conversation and everything seen so far (panel: Forget); a turn in flight is discarded
journalctl --user -fu hermes-companion
```
Keys: Super+Alt+H listen · Super+Alt+E eyes · Super+Alt+S hush. Bar icon: left = panel, right = listen, middle = hush.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Every merge to `main` is what `omarchy plugin update` installs.

