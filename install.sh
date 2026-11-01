#!/usr/bin/env bash
# Installs Simple RSS Reader into your application launcher (per-user, no root).
#   ./install.sh              install
#   ./install.sh --uninstall  remove
set -euo pipefail

APP_DIR="$HOME/.local/share/simple-rss"
DESKTOP_FILE="$HOME/.local/share/applications/simple-rss.desktop"
ICON_FILE="$HOME/.local/share/icons/hicolor/scalable/apps/simple-rss.svg"

refresh_caches() {
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    kbuildsycoca6 >/dev/null 2>&1 || true   # make Plasma re-read launchers now
}

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -rf "$APP_DIR" "$DESKTOP_FILE" "$ICON_FILE"
    refresh_caches
    echo "Removed Simple RSS Reader."
    exit 0
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/simple_rss.py"
[[ -f "$SRC" ]] || { echo "simple_rss.py must be next to install.sh"; exit 1; }

# Use the Python that is active in THIS terminal (so a venv with PySide6 works).
PYTHON="${PYTHON:-$(command -v python3)}"
"$PYTHON" -c "import PySide6, feedparser" 2>/dev/null || {
    echo "Error: $PYTHON can't import PySide6 and feedparser."
    echo "Activate your venv first (or install the packages), then re-run."
    exit 1
}

mkdir -p "$APP_DIR" "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"
install -m 755 "$SRC" "$APP_DIR/simple_rss.py"

cat > "$ICON_FILE" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="14" fill="#f26522"/>
  <circle cx="19" cy="45" r="5" fill="#fff"/>
  <path d="M14 29a21 21 0 0 1 21 21M14 15a35 35 0 0 1 35 35" fill="none"
        stroke="#fff" stroke-width="6" stroke-linecap="round"/>
</svg>
SVG

cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Simple RSS Reader
GenericName=Feed Reader
Comment=Read RSS and Atom feeds
Exec="$PYTHON" "$APP_DIR/simple_rss.py"
Icon=simple-rss
Terminal=false
StartupNotify=true
Categories=Network;News;Qt;
Keywords=RSS;Atom;Feed;News;Reader;
DESKTOP
chmod 644 "$DESKTOP_FILE"

refresh_caches
echo "Installed. Look for 'Simple RSS Reader' in your application launcher."
echo "Using Python: $PYTHON"
