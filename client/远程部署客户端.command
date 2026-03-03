#!/bin/bash
# 远程部署客户端 - macOS 自包含启动脚本
# 双击即可运行，无需额外下载文件

WORK_DIR="$HOME/.remote_deploy_client"
mkdir -p "$WORK_DIR"

echo ""
echo "=================================="
echo "  Remote Deploy - 远程部署客户端"
echo "=================================="

# 检查 Python3
if ! command -v python3 &>/dev/null; then
    echo "[错误] 未找到 python3，请先安装："
    echo "  brew install python3"
    read -p "按回车键退出..."
    exit 1
fi
echo "[OK] Python3: $(python3 --version 2>&1)"

# 安装依赖
python3 -c "import websocket" 2>/dev/null || {
    echo "[安装] 正在安装依赖 websocket-client..."
    python3 -m pip install websocket-client --quiet --break-system-packages 2>/dev/null \
        || python3 -m pip install websocket-client --quiet
}

# 写出 config.py
cat > "$WORK_DIR/config.py" << 'PYEOF'
SERVER_URL = "ws://120.27.152.51:5100/deploy/ws/client"
PYEOF

# 写出 client.py
cat > "$WORK_DIR/client.py" << 'PYEOF'
"""
Remote Deploy Client
====================
Lightweight client that:
- Generates a random 4-char pairing code
- Connects to the deploy server via WebSocket
- Receives shell commands and executes them via subprocess
- Streams stdout/stderr back to the server line by line
- Auto-reconnects on disconnect

Runs in GUI mode (tkinter) if available, otherwise falls back to headless
terminal mode automatically (useful on macOS with system Python's broken Tk).
"""

import json
import os
import platform
import random
import socket
import string
import subprocess
import sys
import threading
import time

import websocket  # websocket-client library

from config import SERVER_URL

# ---------------------------------------------------------------------------
# Try to import tkinter (may fail on macOS system Python with broken Tk)
# On macOS, broken Tk causes SIGABRT which try/except cannot catch.
# So we test Tk in a subprocess - if it crashes, the main process is safe.
# ---------------------------------------------------------------------------
import subprocess as _sp
_tk_test = _sp.run(
    [sys.executable, "-c",
     "import tkinter; r=tkinter.Tk(); r.withdraw(); r.destroy()"],
    capture_output=True, timeout=5
)
_TK_AVAILABLE = (_tk_test.returncode == 0)
if _TK_AVAILABLE:
    import tkinter as tk

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Characters for pairing code - exclude confusing ones: O/0/I/1/L
CODE_CHARS = "".join(
    c for c in string.ascii_uppercase + string.digits
    if c not in "OIL01"
)
CODE_LENGTH = 4

RECONNECT_DELAY = 3  # seconds between reconnect attempts

# ---------------------------------------------------------------------------
# Pairing code generator
# ---------------------------------------------------------------------------

def _code_file() -> str:
    """Path to the saved pairing code file."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".deploy_code")


def generate_code() -> str:
    """Load saved pairing code or generate a new one."""
    path = _code_file()
    if os.path.exists(path):
        try:
            code = open(path, "r").read().strip()
            if len(code) >= CODE_LENGTH and all(c in CODE_CHARS for c in code):
                return code
        except Exception:
            pass
    code = "".join(random.choice(CODE_CHARS) for _ in range(CODE_LENGTH))
    try:
        with open(path, "w") as f:
            f.write(code)
    except Exception:
        pass
    return code


# ---------------------------------------------------------------------------
# System info collector
# ---------------------------------------------------------------------------

def collect_system_info() -> dict:
    """Collect basic system information."""
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "hostname": socket.gethostname(),
    }


# ---------------------------------------------------------------------------
# Base Client (shared WebSocket + command execution logic)
# ---------------------------------------------------------------------------

class _ClientBase:
    """Shared WebSocket and command execution logic."""

    def __init__(self):
        self.code = generate_code()
        self.sys_info = collect_system_info()
        self.ws = None
        self.ws_connected = False
        self.should_run = True

    def _set_status(self, text, color):
        raise NotImplementedError

    def _log(self, message, tag=""):
        raise NotImplementedError

    def _ws_loop(self):
        while self.should_run:
            try:
                self._set_status("Connecting...", "#cccc00")
                self._log("Connecting to server...", "yellow")
                self._connect_ws()
            except Exception as e:
                self._log(f"Connection error: {e}", "red")

            if self.should_run:
                self.ws_connected = False
                self._set_status("Disconnected", "#ff4444")
                self._log(f"Reconnecting in {RECONNECT_DELAY}s...", "yellow")
                time.sleep(RECONNECT_DELAY)

    def _connect_ws(self):
        self.ws = websocket.WebSocketApp(
            SERVER_URL,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self.ws.run_forever(ping_interval=90, ping_timeout=60)

    def _on_ws_open(self, ws):
        self.ws_connected = True
        self._set_status("Connected", "#00ff88")
        self._log("WebSocket connected", "green")
        register_msg = {
            "type": "register",
            "code": self.code,
            "os": self.sys_info["os"],
            "arch": self.sys_info["arch"],
            "hostname": self.sys_info["hostname"],
        }
        try:
            ws.send(json.dumps(register_msg))
            self._log(f"Registered: code={self.code} host={self.sys_info['hostname']}", "gray")
        except Exception as e:
            self._log(f"Register failed: {e}", "red")

    def _on_ws_message(self, ws, message):
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        msg_type = msg.get("type")
        if msg_type == "exec":
            task_id = msg.get("task_id", "")
            cmd = msg.get("cmd", "")
            if task_id and cmd:
                self._log(f"CMD: {cmd[:80]}", "yellow")
                t = threading.Thread(target=self._execute_command, args=(task_id, cmd), daemon=True)
                t.start()
        elif msg_type == "registered":
            self._set_status("Connected", "#00ff88")
            self._log("Server confirmed registration", "green")
        elif msg_type == "ping":
            try:
                ws.send(json.dumps({"type": "pong"}))
                self._log("ping <- server, pong ->", "gray")
            except Exception as e:
                self._log(f"Pong send failed: {e}", "red")
        elif msg_type == "pong":
            self._log("pong <- server", "gray")

    def _on_ws_error(self, ws, error):
        self.ws_connected = False
        self._set_status("Disconnected", "#ff4444")
        self._log(f"WS error: {error}", "red")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        self.ws_connected = False
        self._set_status("Disconnected", "#ff4444")
        self._log(f"WS closed: code={close_status_code} msg={close_msg}", "red")

    def _execute_command(self, task_id, cmd):
        self._set_status("Executing...", "#cccc00")
        try:
            popen_kwargs = {
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
            }
            if sys.platform == "win32":
                popen_kwargs["encoding"] = "utf-8"
                popen_kwargs["errors"] = "replace"
            proc = subprocess.Popen(cmd, **popen_kwargs)
            for line in proc.stdout:
                if not self.should_run:
                    proc.kill()
                    break
                self._send_output(task_id, data=line, done=False)
            proc.wait()
            exit_code = proc.returncode
            self._log(f"CMD done: exit={exit_code}", "gray")
        except Exception as e:
            self._send_output(task_id, data=f"Error: {e}\n", done=False)
            self._log(f"CMD error: {e}", "red")
            exit_code = -1
        self._send_output(task_id, data="", done=True, exit_code=exit_code)
        if self.ws_connected:
            self._set_status("Connected", "#00ff88")

    def _send_output(self, task_id, data, done, exit_code=None):
        msg = {"type": "output", "task_id": task_id, "data": data, "done": done}
        if done:
            msg["exit_code"] = exit_code if exit_code is not None else 0
        try:
            if self.ws and self.ws_connected:
                self.ws.send(json.dumps(msg))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GUI Client (tkinter)
# ---------------------------------------------------------------------------

class DeployClient(_ClientBase):
    def __init__(self):
        super().__init__()
        self.root = tk.Tk()
        self.root.title("远程部署客户端")
        self.root.geometry("420x420")
        self.root.resizable(True, True)
        self.root.configure(bg="#1e1e1e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        tk.Label(self.root, text="Pairing Code", font=("Segoe UI", 12), fg="#888888", bg="#1e1e1e").pack(pady=(20, 5))
        tk.Label(self.root, text=self.code, font=("Consolas", 36, "bold"), fg="#00ff88", bg="#1e1e1e").pack(pady=(0, 6))
        self.label_status = tk.Label(self.root, text="Connecting...", font=("Segoe UI", 10), fg="#cccc00", bg="#1e1e1e")
        self.label_status.pack(pady=(0, 6))
        log_frame = tk.Frame(self.root, bg="#1e1e1e")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self._log_text = tk.Text(log_frame, height=10, bg="#111111", fg="#aaaaaa",
            font=("Consolas", 8), relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD)
        scrollbar = tk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._log_text.tag_configure("green", foreground="#00ff88")
        self._log_text.tag_configure("red", foreground="#ff4444")
        self._log_text.tag_configure("yellow", foreground="#cccc00")
        self._log_text.tag_configure("gray", foreground="#666666")
        threading.Thread(target=self._ws_loop, daemon=True).start()

    def _set_status(self, text, color):
        try:
            self.root.after(0, lambda: self.label_status.config(text=text, fg=color))
        except Exception:
            pass

    def _log(self, message, tag=""):
        try:
            self.root.after(0, self._append_log, message, tag)
        except Exception:
            pass

    def _append_log(self, message, tag):
        try:
            ts = time.strftime("%H:%M:%S")
            line = f"[{ts}] {message}\n"
            self._log_text.configure(state=tk.NORMAL)
            if int(self._log_text.index("end-1c").split(".")[0]) > 200:
                self._log_text.delete("1.0", "50.0")
            if tag:
                self._log_text.insert(tk.END, line, tag)
            else:
                self._log_text.insert(tk.END, line)
            self._log_text.configure(state=tk.DISABLED)
            self._log_text.see(tk.END)
        except Exception:
            pass

    def _on_close(self):
        self.should_run = False
        self.ws_connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Headless Client (terminal fallback)
# ---------------------------------------------------------------------------

class HeadlessClient(_ClientBase):
    def __init__(self):
        super().__init__()
        print()
        print("=" * 40)
        print("   远程部署客户端 (终端模式)")
        print("=" * 40)
        print(f"   配对码:  {self.code}")
        print(f"   主机名:  {self.sys_info['hostname']}")
        print(f"   系统:    {self.sys_info['os']} {self.sys_info['arch']}")
        print("=" * 40)
        print("   按 Ctrl+C 退出")
        print()

    def _set_status(self, text, color):
        print(f"[{time.strftime('%H:%M:%S')}] [状态] {text}")

    def _log(self, message, tag=""):
        print(f"[{time.strftime('%H:%M:%S')}] {message}")

    def run(self):
        threading.Thread(target=self._ws_loop, daemon=True).start()
        try:
            while self.should_run:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在退出...")
            self.should_run = False
            if self.ws:
                try:
                    self.ws.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _TK_AVAILABLE:
        app = DeployClient()
    else:
        print("[警告] tkinter 不可用，切换到终端模式")
        app = HeadlessClient()
    app.run()
PYEOF

echo "[OK] Client ready"
echo ""
cd "$WORK_DIR"
python3 client.py
