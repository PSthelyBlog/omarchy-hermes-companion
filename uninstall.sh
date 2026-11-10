#!/usr/bin/env bash
# Remove everything install.sh set up outside the plugin directory.
# (Run `omarchy plugin remove hermes.companion` afterwards to delete the checkout.)
set -uo pipefail
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Stopping daemon"
systemctl --user disable --now hermes-companion.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/hermes-companion.service"
systemctl --user daemon-reload

echo "==> Removing Hyprland keybinds"
BIND="$HOME/.config/hypr/bindings.lua"
if [[ -f "$BIND" ]] && grep -q "hermes-companion" "$BIND"; then
  python3 - "$BIND" <<'EOF'
import re, sys
p = sys.argv[1]; s = open(p).read()
s = re.sub(r"\n*-- hermes-companion\n(?:o\.bind\(.*companion\.py --ctl.*\)\n)+", "\n", s)
open(p, "w").write(s)
EOF
  hyprctl reload >/dev/null 2>&1 || true
fi

echo "==> Removing runtime state"
rm -rf "${XDG_STATE_HOME:-$HOME/.local/state}/hermes-companion"
rm -f "${XDG_RUNTIME_DIR:-/run/user/$UID}/hermes-companion.sock"
rm -f "$PLUGIN_DIR/.installed"

echo "Done. Delete the plugin with: omarchy plugin remove hermes.companion"
echo "Kept: companion.json (your settings) and Hermes's 'voice' extra, which Hermes's own voice mode"
echo "also uses (remove it with: hermes pm install --without voice)."
