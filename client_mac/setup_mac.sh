#!/bin/bash
# ============================================
# Remote Deploy Client - Mac One-Click Setup
# ============================================
# Usage: curl -sL http://120.27.152.51:5100/deploy/static/setup_mac.sh | bash
# Or:    bash setup_mac.sh
# ============================================

set -e

INSTALL_DIR="$HOME/.remote_deploy"
SERVER="http://120.27.152.51:5100/deploy/static"

echo ""
echo "  =================================="
echo "  Remote Deploy Client - Mac Setup"
echo "  =================================="
echo ""

# --- Check Python 3 ---
if command -v python3 &>/dev/null; then
    PYTHON=python3
    echo "[OK] Python3 found: $(python3 --version)"
else
    echo "[ERROR] Python3 not found."
    echo "  Install via: brew install python3"
    echo "  Or download from: https://www.python.org/downloads/"
    exit 1
fi

# --- Create install directory ---
mkdir -p "$INSTALL_DIR"
echo "[OK] Install directory: $INSTALL_DIR"

# --- Download client files ---
echo "[..] Downloading client files..."
curl -sL "$SERVER/mac/client.py" -o "$INSTALL_DIR/client.py"
curl -sL "$SERVER/mac/config.py" -o "$INSTALL_DIR/config.py"
echo "[OK] Client files downloaded"

# --- Install dependencies ---
echo "[..] Installing dependencies..."
$PYTHON -m pip install --quiet websocket-client 2>/dev/null || \
$PYTHON -m pip install --quiet --user websocket-client 2>/dev/null || \
pip3 install --quiet websocket-client 2>/dev/null
echo "[OK] Dependencies installed"

# --- Create launch script ---
cat > "$INSTALL_DIR/start.command" << 'LAUNCHER'
#!/bin/bash
cd "$(dirname "$0")"
python3 client.py
LAUNCHER
chmod +x "$INSTALL_DIR/start.command"
echo "[OK] Launch script created: $INSTALL_DIR/start.command"

# --- Create desktop shortcut ---
if [ -d "$HOME/Desktop" ]; then
    ln -sf "$INSTALL_DIR/start.command" "$HOME/Desktop/RemoteDeploy.command"
    echo "[OK] Desktop shortcut created"
fi

echo ""
echo "  =================================="
echo "  Setup Complete!"
echo "  =================================="
echo ""
echo "  Start methods:"
echo "    1. Double-click 'RemoteDeploy' on Desktop"
echo "    2. Run: python3 $INSTALL_DIR/client.py"
echo ""
echo "  Starting now..."
echo ""

# --- Launch ---
cd "$INSTALL_DIR"
$PYTHON client.py
