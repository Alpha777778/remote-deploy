"""
Remote Deploy Client (macOS)
============================
Lightweight tkinter GUI that:
- Generates a random 4-char pairing code
- Connects to the deploy server via WebSocket
- Receives shell commands and executes them via subprocess
- Streams stdout/stderr back to the server line by line
- Auto-reconnects on disconnect
"""

import json
import platform
import random
import socket
import string
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont

import websocket  # websocket-client library

from config import SERVER_URL

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

# macOS font preferences (fallback chain)
FONT_UI = "Helvetica Neue"
FONT_MONO = "Menlo"


# ---------------------------------------------------------------------------
# Pairing code generator
# ---------------------------------------------------------------------------

def generate_code() -> str:
    return "".join(random.choice(CODE_CHARS) for _ in range(CODE_LENGTH))


# ---------------------------------------------------------------------------
# System info collector
# ---------------------------------------------------------------------------

def collect_system_info() -> dict:
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "hostname": socket.gethostname(),
    }


# ---------------------------------------------------------------------------
# Client Application
# ---------------------------------------------------------------------------

class DeployClient:

    def __init__(self):
        self.code = generate_code()
        self.sys_info = collect_system_info()
        self.ws = None
        self.ws_connected = False
        self.should_run = True

        # --- Build GUI ---
        self.root = tk.Tk()
        self.root.title("远程部署客户端")
        self.root.geometry("380x220")
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e1e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # macOS: set window to stay on top briefly for visibility
        self.root.attributes("-topmost", True)
        self.root.after(1000, lambda: self.root.attributes("-topmost", False))

        # "Pairing Code" label
        self.label_title = tk.Label(
            self.root,
            text="Pairing Code",
            font=(FONT_UI, 13),
            fg="#888888",
            bg="#1e1e1e",
        )
        self.label_title.pack(pady=(30, 5))

        # Large pairing code
        self.label_code = tk.Label(
            self.root,
            text=self.code,
            font=(FONT_MONO, 40, "bold"),
            fg="#00ff88",
            bg="#1e1e1e",
        )
        self.label_code.pack(pady=(0, 10))

        # Status label
        self.label_status = tk.Label(
            self.root,
            text="Connecting...",
            font=(FONT_UI, 11),
            fg="#cccc00",
            bg="#1e1e1e",
        )
        self.label_status.pack(pady=(0, 5))

        # System info label
        self.label_info = tk.Label(
            self.root,
            text=f"{self.sys_info['os']} / {self.sys_info['arch']} / {self.sys_info['hostname']}",
            font=(FONT_UI, 9),
            fg="#555555",
            bg="#1e1e1e",
        )
        self.label_info.pack(pady=(0, 10))

        # --- Start WebSocket in background ---
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    # ------------------------------------------------------------------
    # GUI helpers
    # ------------------------------------------------------------------

    def _set_status(self, text, color):
        try:
            self.root.after(0, self._update_status_label, text, color)
        except Exception:
            pass

    def _update_status_label(self, text, color):
        try:
            self.label_status.config(text=text, fg=color)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # WebSocket lifecycle
    # ------------------------------------------------------------------

    def _ws_loop(self):
        while self.should_run:
            try:
                self._set_status("Connecting...", "#cccc00")
                self._connect_ws()
            except Exception:
                pass

            if self.should_run:
                self.ws_connected = False
                self._set_status("Disconnected - Reconnecting...", "#ff4444")
                time.sleep(RECONNECT_DELAY)

    def _connect_ws(self):
        self.ws = websocket.WebSocketApp(
            SERVER_URL,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self.ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_ws_open(self, ws):
        self.ws_connected = True
        self._set_status("Connected", "#00ff88")

        register_msg = {
            "type": "register",
            "code": self.code,
            "os": self.sys_info["os"],
            "arch": self.sys_info["arch"],
            "hostname": self.sys_info["hostname"],
        }
        try:
            ws.send(json.dumps(register_msg))
        except Exception:
            pass

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
                t = threading.Thread(
                    target=self._execute_command,
                    args=(task_id, cmd),
                    daemon=True,
                )
                t.start()

        elif msg_type == "registered":
            self._set_status("Connected", "#00ff88")

        elif msg_type == "pong" or msg_type == "ping":
            if msg_type == "ping":
                try:
                    ws.send(json.dumps({"type": "pong"}))
                except Exception:
                    pass

    def _on_ws_error(self, ws, error):
        self.ws_connected = False
        self._set_status("Connection Error", "#ff4444")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        self.ws_connected = False
        self._set_status("Disconnected", "#ff4444")

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

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

            # macOS uses bash by default
            if sys.platform == "darwin":
                popen_kwargs["executable"] = "/bin/bash"

            proc = subprocess.Popen(cmd, **popen_kwargs)

            for line in proc.stdout:
                if not self.should_run:
                    proc.kill()
                    break
                self._send_output(task_id, data=line, done=False)

            proc.wait()
            exit_code = proc.returncode

        except Exception as e:
            self._send_output(task_id, data=f"Error: {e}\n", done=False)
            exit_code = -1

        self._send_output(task_id, data="", done=True, exit_code=exit_code)

        if self.ws_connected:
            self._set_status("Connected", "#00ff88")

    def _send_output(self, task_id, data, done, exit_code=None):
        msg = {
            "type": "output",
            "task_id": task_id,
            "data": data,
            "done": done,
        }
        if done:
            msg["exit_code"] = exit_code if exit_code is not None else 0

        try:
            if self.ws and self.ws_connected:
                self.ws.send(json.dumps(msg))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _on_close(self):
        self.should_run = False
        self.ws_connected = False

        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass

        self.root.destroy()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = DeployClient()
    app.run()
