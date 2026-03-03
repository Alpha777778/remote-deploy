#!/bin/bash
# ============================================
#  Remote Deploy Client for Mac
#  Double-click this file to start
# ============================================

cd "$(dirname "$0")"
INSTALL_DIR="$HOME/.remote_deploy"
mkdir -p "$INSTALL_DIR"

echo ""
echo "  =================================="
echo "  Remote Deploy - 远程部署客户端"
echo "  =================================="
echo ""

# --- Check / Install Python3 ---
if ! command -v python3 &>/dev/null; then
    echo "[!] Python3 not found, attempting to install..."
    echo ""

    # Method 1: Try Homebrew
    if command -v brew &>/dev/null; then
        echo "  Installing Python3 via Homebrew..."
        brew install python3
    else
        # Method 2: Install Homebrew first, then Python
        echo "  Homebrew not found. Installing Homebrew + Python3..."
        echo "  (This may ask for your password)"
        echo ""
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

        # Add Homebrew to PATH for Apple Silicon Macs
        if [ -f "/opt/homebrew/bin/brew" ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        fi

        if command -v brew &>/dev/null; then
            brew install python3
        else
            echo ""
            echo "  [ERROR] Auto-install failed."
            echo "  Please install Python3 manually:"
            echo "  https://www.python.org/downloads/"
            echo ""
            open "https://www.python.org/downloads/"
            echo "  After installing, double-click this file again."
            read -p "  Press Enter to exit..."
            exit 1
        fi
    fi

    # Verify Python3 is now available
    if ! command -v python3 &>/dev/null; then
        echo ""
        echo "  [ERROR] Python3 installation failed."
        echo "  Please install manually: https://www.python.org/downloads/"
        open "https://www.python.org/downloads/"
        read -p "  Press Enter to exit..."
        exit 1
    fi
fi

echo "[OK] Python3: $(python3 --version)"

# --- Install websocket-client if needed ---
python3 -c "import websocket" 2>/dev/null || {
    echo "[..] Installing dependencies..."
    python3 -m pip install --quiet --break-system-packages websocket-client 2>/dev/null || \
    python3 -m pip install --quiet websocket-client 2>/dev/null || \
    python3 -m pip install --quiet --user websocket-client 2>/dev/null || \
    pip3 install --quiet websocket-client 2>/dev/null
    echo "[OK] Dependencies installed"
}

# --- Install tkinter if needed ---
python3 -c "import tkinter" 2>/dev/null || {
    echo "[..] Installing tkinter..."
    if command -v brew &>/dev/null; then
        brew install python-tk@3
    else
        echo "  [WARN] tkinter not available. Trying to continue..."
    fi
}

# --- Write config ---
cat > "$INSTALL_DIR/config.py" << 'PYEOF'
SERVER_URL = "ws://120.27.152.51:5100/deploy/ws/client"
PYEOF

# --- Write client code ---
cat > "$INSTALL_DIR/client.py" << 'PYEOF'
import json, os, platform, random, socket, string, subprocess, sys, threading, time, tkinter as tk
import websocket
from config import SERVER_URL

CODE_CHARS = "".join(c for c in string.ascii_uppercase + string.digits if c not in "OIL01")
CODE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".deploy_code")

def generate_code():
    if os.path.exists(CODE_FILE):
        try:
            code = open(CODE_FILE).read().strip()
            if len(code) >= 4 and all(c in CODE_CHARS for c in code):
                return code
        except: pass
    code = "".join(random.choice(CODE_CHARS) for _ in range(4))
    try:
        with open(CODE_FILE, "w") as f: f.write(code)
    except: pass
    return code

def collect_system_info():
    return {"os": platform.system(), "arch": platform.machine(), "hostname": socket.gethostname()}

class DeployClient:
    def __init__(self):
        self.code = generate_code()
        self.sys_info = collect_system_info()
        self.ws = None
        self.ws_connected = False
        self.should_run = True

        self.root = tk.Tk()
        self.root.title("远程部署客户端")
        self.root.geometry("380x230")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e1e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.attributes("-topmost", True)
        self.root.after(1000, lambda: self.root.attributes("-topmost", False))

        tk.Label(self.root, text="Pairing Code", font=("Helvetica Neue", 13), fg="#888888", bg="#1e1e1e").pack(pady=(30, 5))
        self.label_code = tk.Label(self.root, text=self.code, font=("Menlo", 40, "bold"), fg="#00ff88", bg="#1e1e1e")
        self.label_code.pack(pady=(0, 10))
        self.label_status = tk.Label(self.root, text="Connecting...", font=("Helvetica Neue", 11), fg="#cccc00", bg="#1e1e1e")
        self.label_status.pack(pady=(0, 5))
        tk.Label(self.root, text=f"{self.sys_info['os']} / {self.sys_info['arch']} / {self.sys_info['hostname']}", font=("Helvetica Neue", 9), fg="#555555", bg="#1e1e1e").pack(pady=(0, 10))

        threading.Thread(target=self._ws_loop, daemon=True).start()

    def _set_status(self, text, color):
        try: self.root.after(0, lambda: self.label_status.config(text=text, fg=color))
        except: pass

    def _ws_loop(self):
        while self.should_run:
            try:
                self._set_status("Connecting...", "#cccc00")
                ws = websocket.WebSocketApp(SERVER_URL, on_open=self._on_ws_open, on_message=self._on_ws_message, on_error=self._on_ws_error, on_close=self._on_ws_close)
                self.ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except: pass
            if self.should_run:
                self.ws_connected = False
                self._set_status("Disconnected - Reconnecting...", "#ff4444")
                time.sleep(3)

    def _on_ws_open(self, ws):
        self.ws_connected = True
        self._set_status("Connected", "#00ff88")
        try: ws.send(json.dumps({"type": "register", "code": self.code, **self.sys_info}))
        except: pass

    def _on_ws_message(self, ws, message):
        try: msg = json.loads(message)
        except: return
        t = msg.get("type")
        if t == "exec":
            tid, cmd = msg.get("task_id", ""), msg.get("cmd", "")
            if tid and cmd: threading.Thread(target=self._exec, args=(tid, cmd), daemon=True).start()
        elif t == "ping":
            try: ws.send(json.dumps({"type": "pong"}))
            except: pass

    def _on_ws_error(self, ws, error):
        self.ws_connected = False
        self._set_status("Connection Error", "#ff4444")

    def _on_ws_close(self, ws, *a):
        self.ws_connected = False
        self._set_status("Disconnected", "#ff4444")

    def _exec(self, task_id, cmd):
        self._set_status("Executing...", "#cccc00")
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, executable="/bin/bash")
            for line in proc.stdout:
                if not self.should_run: proc.kill(); break
                self._send(task_id, line, False)
            proc.wait()
            ec = proc.returncode
        except Exception as e:
            self._send(task_id, f"Error: {e}\n", False)
            ec = -1
        self._send(task_id, "", True, ec)
        if self.ws_connected: self._set_status("Connected", "#00ff88")

    def _send(self, task_id, data, done, exit_code=None):
        msg = {"type": "output", "task_id": task_id, "data": data, "done": done}
        if done: msg["exit_code"] = exit_code if exit_code is not None else 0
        try:
            if self.ws and self.ws_connected: self.ws.send(json.dumps(msg))
        except: pass

    def _on_close(self):
        self.should_run = False
        if self.ws:
            try: self.ws.close()
            except: pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    DeployClient().run()
PYEOF

echo "[OK] Client ready"
echo ""

# --- Launch ---
cd "$INSTALL_DIR"
python3 client.py
