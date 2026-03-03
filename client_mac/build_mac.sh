#!/bin/bash
# ============================================
# Build macOS .app bundle with PyInstaller
# ============================================
# Run on a Mac: bash build_mac.sh
# Output: dist/RemoteDeploy.app
# ============================================

set -e

echo "Building RemoteDeploy.app..."

# Install build dependencies
pip3 install pyinstaller websocket-client

# Build .app bundle
pyinstaller \
    --onefile \
    --windowed \
    --name "RemoteDeploy" \
    --add-data "config.py:." \
    client.py

echo ""
echo "Build complete!"
echo "App location: dist/RemoteDeploy.app (or dist/RemoteDeploy)"
echo ""
echo "To distribute, zip the app:"
echo "  cd dist && zip -r RemoteDeploy-Mac.zip RemoteDeploy.app"
