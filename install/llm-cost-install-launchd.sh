#!/usr/bin/env bash
# Install the LLM Cost forwarder as a macOS LaunchAgent (runs at login, restarts on exit).
#
#   AGD_URL=https://agd.example.com AGD_LLM_COST_TOKEN=agdlc_... bash llm-cost-install-launchd.sh
#   bash llm-cost-install-launchd.sh --uninstall
#
# The token is written to a mode-600 file under ~/Library/Application Support/agd-llm-cost.
set -euo pipefail

LABEL="com.agd.llm-cost-forwarder"
INSTALL_DIR="${AGD_LLM_COST_DIR:-$HOME/Library/Application Support/agd-llm-cost}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL (files kept in $INSTALL_DIR)"
  exit 0
fi

: "${AGD_URL:?set AGD_URL to the dashboard base URL}"
AGD_URL="${AGD_URL%/}"
case "$AGD_URL" in http://*|https://*) ;; *) echo "AGD_URL must be http(s)" >&2; exit 1 ;; esac

PYTHON="${AGD_PYTHON:-$(command -v python3 || true)}"
[[ -x "$PYTHON" ]] || { echo "python3 not found; install Xcode command line tools or set AGD_PYTHON" >&2; exit 1; }

mkdir -p "$INSTALL_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$INSTALL_DIR"

if [[ -f "$HERE/llm-cost-forward.py" ]]; then
  cp "$HERE/llm-cost-forward.py" "$INSTALL_DIR/llm-cost-forward.py"
else
  curl -fsSL "$AGD_URL/api/llm-cost/forwarder/llm-cost-forward.py" -o "$INSTALL_DIR/llm-cost-forward.py"
fi

if [[ -n "${AGD_LLM_COST_TOKEN:-}" ]]; then
  umask 077
  printf '%s' "$AGD_LLM_COST_TOKEN" > "$INSTALL_DIR/token"
  chmod 600 "$INSTALL_DIR/token"
elif [[ ! -f "$INSTALL_DIR/token" ]]; then
  echo "set AGD_LLM_COST_TOKEN (shown once when you create the device in LLM Cost > Devices)" >&2
  exit 1
fi

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
PY_X="$(printf '%s' "$PYTHON" | xml_escape)"
DIR_X="$(printf '%s' "$INSTALL_DIR" | xml_escape)"
URL_X="$(printf '%s' "$AGD_URL" | xml_escape)"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY_X</string>
    <string>$DIR_X/llm-cost-forward.py</string>
    <string>--url</string><string>$URL_X</string>
    <string>--token-file</string><string>$DIR_X/token</string>
    <string>--interval</string><string>60</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DIR_X/forwarder.log</string>
  <key>StandardErrorPath</key><string>$DIR_X/forwarder.log</string>
</dict>
</plist>
PLIST

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
echo "installed $LABEL -> $AGD_URL"
echo "log: $INSTALL_DIR/forwarder.log"
