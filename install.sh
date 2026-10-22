#!/usr/bin/env bash
# Install the feishu-model-picker plugin into a Hermes profile. Idempotent.
# Run from the extracted archive root:  ./install.sh
set -euo pipefail

NAME="feishu-model-picker"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins"
DST="$PLUGINS_DIR/$NAME"

echo "source  : $SRC/$NAME"
echo "plugins : $PLUGINS_DIR"

[ -f "$SRC/$NAME/__init__.py" ] || {
  echo "ERROR: $SRC/$NAME/__init__.py not found." >&2
  echo "Run this from the extracted archive; expected layout:" >&2
  echo "  <here>/$NAME/__init__.py" >&2
  exit 1
}
[ -f "$SRC/$NAME/plugin.yaml" ] || {
  echo "ERROR: $SRC/$NAME/plugin.yaml not found — bad archive." >&2
  exit 1
}

mkdir -p "$PLUGINS_DIR"
if [ -d "$DST" ]; then
  echo "replacing existing $DST"
  rm -rf "$DST"
fi

if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude '__pycache__' "$SRC/$NAME/" "$DST/"
else
  mkdir -p "$DST"
  cp -a "$SRC/$NAME/." "$DST/"
  rm -rf "$DST/__pycache__"
fi

if [ ! -f "$DST/__init__.py" ] || [ ! -f "$DST/plugin.yaml" ]; then
  echo "ERROR: install incomplete — __init__.py / plugin.yaml missing at $DST" >&2
  exit 1
fi

echo
echo "installed -> $DST"
find "$DST" -type f | sed "s|$DST/|  |" | sort

cat <<EOF

Next steps:
  1) Enable the plugin for this profile:
       hermes config set plugins.enabled '["$NAME"]'
     (multi-profile: prefix with HERMES_HOME=/path/to/profiles/<name>)
  2) Configure the Feishu bot custom menu — see
       $DST/docs/feishu-menu-setup.md
  3) Restart the gateway so the plugin loads:
       hermes gateway restart
EOF
