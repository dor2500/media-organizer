#!/usr/bin/env bash
# Media Organizer - One-line macOS / Linux Installer
#   curl -fsSL https://raw.githubusercontent.com/dor2500/media-organizer/main/install.sh | bash
set -euo pipefail

REPO="dor2500/media-organizer"
OS="$(uname -s)"
INSTALL_DIR="${HOME}/.local/share/media-organizer"
BIN_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

API="https://api.github.com/repos/${REPO}/releases/latest"
JSON="$(curl -fsSL "$API")"

if [ "$OS" = "Darwin" ]; then
    URL="$(printf '%s' "$JSON" | grep -oE 'https://[^"]+MediaOrganizer-macos\.zip' | head -n1 || true)"
    [ -n "$URL" ] || { echo "No macOS binary found in latest release yet."; exit 1; }
    TMP="$(mktemp -d)"
    curl -fsSL "$URL" -o "$TMP/app.zip"
    unzip -oq "$TMP/app.zip" -d "$INSTALL_DIR"
    rm -rf "$TMP"
    ln -sf "$INSTALL_DIR/MediaOrganizer-cli" "$BIN_DIR/media-organizer-cli"
    echo "Installed! Open: $INSTALL_DIR/MediaOrganizer.app  (or CLI: media-organizer-cli)"
else
    URL="$(printf '%s' "$JSON" | grep -oE 'https://[^"]+MediaOrganizer-linux\.tar\.gz' | head -n1 || true)"
    [ -n "$URL" ] || { echo "No Linux binary found in latest release yet."; exit 1; }
    TMP="$(mktemp -d)"
    curl -fsSL "$URL" -o "$TMP/app.tar.gz"
    tar -xzf "$TMP/app.tar.gz" -C "$INSTALL_DIR"
    rm -rf "$TMP"
    chmod +x "$INSTALL_DIR/MediaOrganizer" "$INSTALL_DIR/MediaOrganizer-cli"
    ln -sf "$INSTALL_DIR/MediaOrganizer" "$BIN_DIR/media-organizer"
    ln -sf "$INSTALL_DIR/MediaOrganizer-cli" "$BIN_DIR/media-organizer-cli"
    echo "Installed to $INSTALL_DIR (linked in $BIN_DIR). Run: media-organizer"
fi
