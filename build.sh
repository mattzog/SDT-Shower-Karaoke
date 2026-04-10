#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Karaoke Player — build script
# Run this on your own Linux machine to produce a portable
# single-file executable that matches your system's glibc.
#
# Usage:
#   chmod +x build.sh
#   ./build.sh
#
# Output:
#   ./karaoke_player   (standalone executable)
# ─────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "═══════════════════════════════════════════"
echo "  Karaoke Player — build"
echo "═══════════════════════════════════════════"

# ── Check Python ──────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install it with:"
    echo "  sudo apt install python3 python3-pip"
    exit 1
fi

PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "Python $PYVER found."

if python3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
    echo "✓ Python version OK"
else
    echo "ERROR: Python 3.10+ required (you have $PYVER)"
    exit 1
fi

# ── Install pip dependencies ──────────────────
echo ""
echo "Installing dependencies..."
pip3 install --quiet --upgrade pygame mutagen pyinstaller

# ── Locate pyinstaller ────────────────────────
# pip may install scripts to ~/.local/bin which isn't always in PATH
if command -v pyinstaller &>/dev/null; then
    PYINSTALLER="pyinstaller"
elif [ -f "$HOME/.local/bin/pyinstaller" ]; then
    PYINSTALLER="$HOME/.local/bin/pyinstaller"
else
    # Ask Python where it installed scripts
    SCRIPTS="$(python3 -c "import sysconfig; print(sysconfig.get_path('scripts'))")"
    PYINSTALLER="$SCRIPTS/pyinstaller"
fi

if ! command -v "$PYINSTALLER" &>/dev/null && [ ! -f "$PYINSTALLER" ]; then
    echo "ERROR: pyinstaller not found after install. Try:"
    echo "  pip3 install pyinstaller"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo "  ./build.sh"
    exit 1
fi

echo "Using: $PYINSTALLER"

# ── Build ─────────────────────────────────────
echo ""
echo "Building executable..."

cd "$SCRIPT_DIR"

"$PYINSTALLER" \
    --onefile \
    --name karaoke_player \
    --hidden-import mutagen.mp3 \
    --hidden-import mutagen.id3 \
    --hidden-import mutagen.oggvorbis \
    --hidden-import mutagen.flac \
    --noconfirm \
    --clean \
    karaoke_player.py

# ── Move result ───────────────────────────────
if [ -f "$SCRIPT_DIR/dist/karaoke_player" ]; then
    mv "$SCRIPT_DIR/dist/karaoke_player" "$SCRIPT_DIR/karaoke_player"
    rm -rf "$SCRIPT_DIR/dist" "$SCRIPT_DIR/build" "$SCRIPT_DIR/karaoke_player.spec"
    echo ""
    echo "═══════════════════════════════════════════"
    echo "  ✓ Build complete!"
    echo ""
    echo "  Run with:   ./karaoke_player"
    echo "  Or:         ./karaoke_player /path/to/music"
    echo ""
    echo "  Note: install VLC for MP4/MKV support:"
    echo "        sudo apt install vlc"
    echo "═══════════════════════════════════════════"
else
    echo "ERROR: Build failed — check output above."
    exit 1
fi
