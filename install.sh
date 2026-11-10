#!/usr/bin/env bash
# Install / update the Hermes Companion plugin on this Omarchy machine.
set -uo pipefail
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="$(basename "$PLUGIN_DIR")"
UNIT_DIR="$HOME/.config/systemd/user"
HERMES_MIN="0.21.7"
# The daemon and every `--ctl` command start on the system Python. `--ctl` needs only the standard
# library; the daemon imports Hermes's bootstrap, which moves it onto Hermes's own environment.
SYS_PY=/usr/bin/python3

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
die()   { red "✗ $*"; exit 1; }
ask()   { local a; read -r -p "$1 [Y/n] " a; [[ -z "$a" || "$a" =~ ^[Yy] ]]; }

# ---------------------------------------------------------------- 1. Omarchy tooling
echo "==> Checking Omarchy tooling"
declare -A PKG_OF=( [grim]=grim [hyprctl]=hyprland [notify-send]=libnotify [pw-record]=pipewire [pactl]=libpulse [wpctl]=wireplumber [systemctl]=systemd )
missing=()
for c in omarchy omarchy-shell grim hyprctl notify-send pw-record pactl wpctl systemctl; do
  command -v "$c" >/dev/null 2>&1 || missing+=("$c")
done
if ((${#missing[@]})); then
  yellow "Missing commands: ${missing[*]}"
  for c in omarchy omarchy-shell; do
    if [[ " ${missing[*]} " == *" $c "* ]]; then
      [[ -x /usr/share/omarchy/bin/$c ]] && die "'$c' exists but is not on PATH — add /usr/share/omarchy/bin to PATH (e.g. in ~/.bashrc) and re-run."
      die "'$c' not found — this plugin requires Omarchy 4.x (https://omarchy.org)."
    fi
  done
  pkgs=(); for c in "${missing[@]}"; do pkgs+=("${PKG_OF[$c]:-$c}"); done
  if ask "Install the missing packages with 'omarchy pkg add ${pkgs[*]}'?"; then
    omarchy pkg add "${pkgs[@]}" || die "package install failed"
    for c in "${missing[@]}"; do command -v "$c" >/dev/null 2>&1 || die "'$c' still not on PATH after install"; done
  else
    die "cannot continue without: ${missing[*]}"
  fi
fi
green "✓ Omarchy tooling present"

# ---------------------------------------------------------------- 2. Hermes installed
echo "==> Checking Hermes Agent"
HERMES_DIR="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
hermes_ok() { [[ -f "$HERMES_DIR/run_agent.py" ]]; }
if ! hermes_ok; then
  yellow "Hermes Agent not found at $HERMES_DIR"
  echo "The companion runs inside Hermes' Python environment. Omarchy can install it:"
  echo "  1) omarchy install ai hermes   — Hermes desktop app + runtime (recommended)"
  echo "  2) omarchy install hermes cli  — Hermes CLI only"
  echo "  3) abort (set HERMES_AGENT_DIR if Hermes lives elsewhere)"
  read -r -p "Choice [1/2/3]: " choice </dev/tty
  case "${choice:-1}" in
    1) omarchy install ai hermes || die "Hermes install failed" ;;
    2) omarchy install hermes cli --now || die "Hermes install failed" ;;
    *) exit 1 ;;
  esac
  hermes_ok || die "Hermes still not found at $HERMES_DIR. Run 'hermes' once to finish its setup, then re-run this installer."
fi
[[ -x "$SYS_PY" ]] || die "$SYS_PY not found — install it with 'omarchy pkg add python', then re-run this installer."

# Since 0.21.7 Hermes runs from environments its package manager (PM) builds under
# ~/.hermes/installs/, and rebuilds them from its own uv.lock on updates. Older releases, including
# the 2026.9.x one Omarchy's hermes-desktop package installs, keep a venv inside the checkout and
# have no PM. The companion asks PM for its voice dependencies, so it needs a PM-era Hermes.
[[ -f "$HERMES_DIR/pm/environments.py" ]] \
  || die "Hermes at $HERMES_DIR predates its package manager. Run 'hermes update', then re-run this installer."

# The checkout's own launcher runs Hermes on the Python that PM manages. Checkouts without one
# (developer checkouts) run their `hermes` script, whose bootstrap selects the environment.
if [[ -x "$HERMES_DIR/.hermes/bin/hermes" ]]; then HERMES=("$HERMES_DIR/.hermes/bin/hermes")
else HERMES=("$SYS_PY" "$HERMES_DIR/hermes"); fi

# Run Python code inside Hermes's environment exactly as the daemon does: import hermes_bootstrap
# first. It selects and leases the dependency environment PM committed, re-executing into Hermes's
# own Python when this one is not it, so the checks below test what the daemon will really load.
hermes_py() { (cd "$HERMES_DIR" && "$SYS_PY" -c $'import hermes_bootstrap\n'"$1"); }

# ---------------------------------------------------------------- 3. version + import smoke test
ver="$("${HERMES[@]}" --version 2>/dev/null | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -1 | tr -d v)"
[[ -n "$ver" ]] || ver="0.0.0"
if [[ "$(printf '%s\n%s\n' "$HERMES_MIN" "$ver" | sort -V | head -1)" != "$HERMES_MIN" ]]; then
  die "Hermes $ver found, but >= $HERMES_MIN is required. Run 'hermes update'."
fi
green "✓ Hermes $ver at $HERMES_DIR"

if ! err="$(hermes_py 'import run_agent, hermes_cli.voice, tools.tts_tool, tools.transcription_tools, agent.models_dev, hermes_cli.models' 2>&1 >/dev/null)"; then
  red "Hermes import check failed:"; echo "$err" | tail -3
  die "the Hermes install looks broken — try 'hermes update' or 'hermes doctor'"
fi
green "✓ Hermes modules import"

# ---------------------------------------------------------------- 4. providers (informational)
providers="$(hermes_py '
from hermes_cli.models import list_available_providers
names = [p["id"] for p in list_available_providers() if p.get("authenticated")]
try:
    from agent.anthropic_credentials import resolve_anthropic_token
    if resolve_anthropic_token() and "anthropic" not in names: names.append("anthropic")
except Exception: pass
print(" ".join(names))
' 2>/dev/null)"
if [[ -n "$providers" ]]; then green "✓ Model providers with credentials: $providers"
else yellow "! No model provider credentials yet. The companion will start idle; run 'hermes auth add <provider>' (or log in to Claude Code), then pick a vision model in the widget."; fi

# ---------------------------------------------------------------- 5. voice dependencies + STT
# faster-whisper and sounddevice come from Hermes's own `voice` extra. PM installs it from Hermes's
# locked uv.lock, at the versions Hermes tested with the rest of its packages, and keeps it through
# every `hermes update`. The plugin installs no Python packages of its own: PM rebuilds Hermes's
# environment from that lock on updates, so anything installed beside it would vanish, and a lock
# kept here could only ever match one Hermes release. Pillow is a Hermes core dependency.
echo "==> Voice dependencies (Hermes 'voice' extra, installed by Hermes)"
"${HERMES[@]}" pm install --extra voice \
  || die "Hermes could not install its voice extra (see above). Fix that, or run 'hermes pm install --extra voice', then re-run this installer."
if hermes_py 'import faster_whisper, sounddevice, PIL' 2>/dev/null; then green "✓ Local speech-to-text ready"
else yellow "! faster-whisper/sounddevice/PIL not importable in Hermes's environment — voice requests disabled until fixed"; fi

# ---------------------------------------------------------------- 6. install
echo "==> Plugin config"
[[ -f "$PLUGIN_DIR/companion.json" ]] || cp "$PLUGIN_DIR/companion.example.json" "$PLUGIN_DIR/companion.json"
"$SYS_PY" - "$PLUGIN_DIR/companion.json" "$HERMES_DIR" <<'EOF'
import json, sys
p, hermes = sys.argv[1], sys.argv[2]
d = json.load(open(p)); d["hermes_dir"] = hermes
open(p, "w").write(json.dumps(d, indent=2) + "\n")
EOF

echo "==> systemd --user unit"
mkdir -p "$UNIT_DIR"
rm -f "$UNIT_DIR/hermes-companion.service"   # older installs symlinked a static unit
sed "s#@HERMES_DIR@#$HERMES_DIR#g; s#@PLUGIN_DIR@#$PLUGIN_DIR#g" "$PLUGIN_DIR/hermes-companion.service.in" > "$UNIT_DIR/hermes-companion.service"
systemctl --user daemon-reload
systemctl --user enable hermes-companion.service >/dev/null 2>&1

echo "==> Hyprland keybinds"
BIND="$HOME/.config/hypr/bindings.lua"
CTL="$SYS_PY $PLUGIN_DIR/daemon/companion.py --ctl"
# Older installs bound the keys to the venv inside the Hermes checkout, which PM-era Hermes no
# longer creates or updates. Point them at the system Python instead.
OLD_CTL="$HERMES_DIR/venv/bin/python $PLUGIN_DIR/daemon/companion.py --ctl"
if grep -qF "$OLD_CTL" "$BIND" 2>/dev/null; then
  "$SYS_PY" - "$BIND" "$OLD_CTL" "$CTL" <<'EOF'
import sys
p, old, new = sys.argv[1:4]
s = open(p).read()
open(p, "w").write(s.replace(old, new))
EOF
  hyprctl reload >/dev/null 2>&1 || true
fi
if ! grep -q "hermes-companion" "$BIND" 2>/dev/null; then
  cat >> "$BIND" <<EOF

-- hermes-companion
o.bind("SUPER + ALT + H", "Hermes: listen", "$CTL listen")
o.bind("SUPER + ALT + E", "Hermes: toggle eyes", "$CTL toggle-eyes")
o.bind("SUPER + ALT + S", "Hermes: hush", "$CTL hush")
EOF
  hyprctl reload >/dev/null 2>&1 || true
fi

echo "==> Shell plugin"
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
omarchy plugin enable "$PLUGIN_ID" right >/dev/null 2>&1 || true

echo "==> Start"
systemctl --user restart hermes-companion.service
sleep 2
systemctl --user --no-pager status hermes-companion.service | head -3
touch "$PLUGIN_DIR/.installed"
green "Done. Right-click the eye icon (or Super+Alt+H) to talk. Logs: journalctl --user -fu hermes-companion"
