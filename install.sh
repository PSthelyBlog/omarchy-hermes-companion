#!/usr/bin/env bash
# Install / update the Hermes Companion plugin on this Omarchy machine.
set -uo pipefail
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ID="$(basename "$PLUGIN_DIR")"
UNIT_DIR="$HOME/.config/systemd/user"
HERMES_MIN="0.21.0"

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
hermes_ok() { [[ -f "$HERMES_DIR/run_agent.py" && -x "$HERMES_DIR/venv/bin/python" ]]; }
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
PY="$HERMES_DIR/venv/bin/python"

# ---------------------------------------------------------------- 3. version + import smoke test
ver="$(cd "$HERMES_DIR" && "$PY" hermes --version 2>/dev/null | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -1 | tr -d v)"
[[ -n "$ver" ]] || ver="0.0.0"
if [[ "$(printf '%s\n%s\n' "$HERMES_MIN" "$ver" | sort -V | head -1)" != "$HERMES_MIN" ]]; then
  die "Hermes $ver found, but >= $HERMES_MIN is required. Run 'hermes update'."
fi
green "✓ Hermes $ver at $HERMES_DIR"

if ! err="$(cd "$HERMES_DIR" && "$PY" -c 'import run_agent, hermes_cli.voice, tools.tts_tool, tools.transcription_tools, agent.models_dev, hermes_cli.models' 2>&1 >/dev/null)"; then
  red "Hermes import check failed:"; echo "$err" | tail -3
  die "the Hermes install looks broken — try 'hermes update' or 'hermes doctor'"
fi
green "✓ Hermes modules import"

# ---------------------------------------------------------------- 4. providers (informational)
providers="$(cd "$HERMES_DIR" && "$PY" - <<'EOF' 2>/dev/null
import sys; sys.path.insert(0, ".")
from hermes_cli.models import list_available_providers
names = [p["id"] for p in list_available_providers() if p.get("authenticated")]
try:
    from agent.anthropic_credentials import resolve_anthropic_token
    if resolve_anthropic_token() and "anthropic" not in names: names.append("anthropic")
except Exception: pass
print(" ".join(names))
EOF
)"
if [[ -n "$providers" ]]; then green "✓ Model providers with credentials: $providers"
else yellow "! No model provider credentials yet. The companion will start idle; run 'hermes auth add <provider>' (or log in to Claude Code), then pick a vision model in the widget."; fi

# ---------------------------------------------------------------- 5. python deps + STT
# Installed from requirements.lock: every direct and transitive package is pinned to an exact
# version with sha256 hashes for each accepted artifact. --require-hashes makes uv refuse any
# package that is unpinned, unhashed or whose artifact does not match, so a newly published
# (or tampered) release cannot silently execute inside the persistent Hermes environment.
# A failure here is a supply-chain signal: abort instead of continuing with unverified code.
#
# uv runs from $HERMES_DIR on purpose, so it inherits [tool.uv] from Hermes's pyproject.toml.
# If Hermes force-pins (override-dependencies) a package this lock also pins, the forced version
# has no trusted hash here and the install aborts. That is the intended fail-closed outcome: with
# --no-config, uv would instead silently install this lock's version over Hermes's security pin.
# pillow is deliberately NOT in this lock: Hermes core provides and pins it (Pillow==13.0.0 for
# its 13.0.0 security advisory set), and a plugin must not hash-pin a different version against
# that. The import check below still verifies PIL is present.
echo "==> Python deps (into Hermes venv, hash-verified from requirements.lock)"
( cd "$HERMES_DIR" && source venv/bin/activate \
  && uv pip install -q --require-hashes -r "$PLUGIN_DIR/requirements.lock" ) \
  || die "dependency install failed — hash-verified install refused (see requirements.lock)"
if (cd "$HERMES_DIR" && "$PY" -c 'import faster_whisper, sounddevice, PIL' 2>/dev/null); then green "✓ Local speech-to-text ready"
else yellow "! faster-whisper/sounddevice/pillow not importable — voice requests disabled until fixed"; fi

# ---------------------------------------------------------------- 6. install
echo "==> Plugin config"
[[ -f "$PLUGIN_DIR/companion.json" ]] || cp "$PLUGIN_DIR/companion.example.json" "$PLUGIN_DIR/companion.json"
"$PY" - "$PLUGIN_DIR/companion.json" "$HERMES_DIR" <<'EOF'
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
CTL="$PY $PLUGIN_DIR/daemon/companion.py --ctl"
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
