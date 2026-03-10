#!/usr/bin/env python3
"""
Remote Deploy Client - 纯标准库版，无需任何外部依赖
Python 3.6+ 即可运行
"""
import base64, json, os, platform, random, socket, string, struct
import subprocess, sys, threading, time

SERVER_HOST = "120.27.152.51"
SERVER_PORT = 5100
SERVER_PATH = "/deploy/ws/client"

CODE_CHARS = "".join(c for c in string.ascii_uppercase + string.digits if c not in "OIL01")
CODE_FILE   = os.path.expanduser("~/.remote_deploy/.deploy_code")

# ── 配对码 ──────────────────────────────────────────────────────────────────

def generate_code():
    if os.path.exists(CODE_FILE):
        try:
            code = open(CODE_FILE).read().strip()
            if len(code) >= 4 and all(c in CODE_CHARS for c in code):
                return code
        except: pass
    code = "".join(random.choice(CODE_CHARS) for _ in range(4))
    try:
        os.makedirs(os.path.dirname(CODE_FILE), exist_ok=True)
        with open(CODE_FILE, "w") as f: f.write(code)
    except: pass
    return code

# ── WebSocket 实现（纯 stdlib）────────────────────────────────────────────────

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf += chunk
    return buf

def ws_connect(host, port, path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(30)
    sock.connect((host, port))
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("Handshake failed: server closed")
        resp += chunk
    if b"101" not in resp:
        raise ConnectionError(f"Handshake rejected: {resp[:100]}")
    sock.settimeout(None)
    return sock

def ws_send(sock, text):
    data = text.encode() if isinstance(text, str) else text
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    n = len(data)
    if n < 126:
        hdr = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x81, 0xFE, n)
    else:
        hdr = struct.pack("!BBQ", 0x81, 0xFF, n)
    sock.sendall(hdr + mask + masked)

def ws_recv(sock):
    """返回 (opcode, bytes)，opcode=None 表示 ping/pong 已处理"""
    hdr = _recv_exact(sock, 2)
    opcode = hdr[0] & 0x0F
    is_masked = (hdr[1] & 0x80) != 0
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if is_masked else None
    raw = _recv_exact(sock, length)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(raw)) if mask else raw
    if opcode == 0x9:          # ping → pong (客户端帧必须加掩码)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x8A, 0x80 | n)
        else:
            hdr = struct.pack("!BBH", 0x8A, 0xFE, n)
        sock.sendall(hdr + mask + masked)
        return None, None
    if opcode == 0x0A:         # pong (服务器回应我们的ping，忽略即可)
        return None, None
    if opcode == 0x8:          # close
        raise ConnectionError("Server closed WebSocket")
    return opcode, data

# ── 主客户端 ─────────────────────────────────────────────────────────────────

class DeployClient:
    def __init__(self):
        self.code      = generate_code()
        self.sock      = None
        self.lock      = threading.Lock()
        self.should_run = True

        print("")
        print("  ╔══════════════════════════════════╗")
        print(f"  ║   配对码: {self.code:<26}║")
        print("  ╚══════════════════════════════════╝")
        print(f"  设备: {platform.system()} / {platform.machine()} / {socket.gethostname()}")
        print("  按 Ctrl+C 退出")
        print("")

    def _send(self, obj):
        with self.lock:
            if self.sock:
                try:
                    ws_send(self.sock, json.dumps(obj))
                    return True
                except: pass
        return False

    def _exec(self, task_id, cmd):
        try:
            proc = subprocess.Popen(
                cmd, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, executable="/bin/bash"
            )
            for line in proc.stdout:
                if not self.should_run: proc.kill(); break
                self._send({"type": "output", "task_id": task_id, "data": line, "done": False})
            proc.wait()
            ec = proc.returncode
        except Exception as e:
            self._send({"type": "output", "task_id": task_id, "data": f"Error: {e}\n", "done": False})
            ec = -1
        self._send({"type": "output", "task_id": task_id, "data": "", "done": True, "exit_code": ec})

    def _loop(self):
        while self.should_run:
            try:
                self._status("正在连接...")
                sock = ws_connect(SERVER_HOST, SERVER_PORT, SERVER_PATH)
                with self.lock:
                    self.sock = sock
                ws_send(sock, json.dumps({
                    "type": "register", "code": self.code,
                    "os": platform.system(), "arch": platform.machine(),
                    "hostname": socket.gethostname(),
                }))
                self._status("已连接 - 等待指令")
                # 心跳线程：每10秒发一个WebSocket PING帧防止连接超时
                def _heartbeat():
                    while self.should_run:
                        time.sleep(10)
                        with self.lock:
                            if self.sock:
                                try:
                                    mask = os.urandom(4)
                                    self.sock.sendall(struct.pack("!BB", 0x89, 0x80) + mask)  # masked ping
                                except: pass
                threading.Thread(target=_heartbeat, daemon=True).start()
                sock.settimeout(60)
                while self.should_run:
                    try:
                        opcode, data = ws_recv(sock)
                    except socket.timeout:
                        continue
                    if opcode is None:
                        continue
                    try:
                        msg = json.loads(data)
                    except: continue
                    t = msg.get("type")
                    if t == "exec":
                        tid, cmd = msg.get("task_id", ""), msg.get("cmd", "")
                        if tid and cmd:
                            threading.Thread(target=self._exec, args=(tid, cmd), daemon=True).start()
                    elif t == "ping":
                        self._send({"type": "pong"})
            except Exception:
                pass
            finally:
                with self.lock:
                    if self.sock:
                        try: self.sock.close()
                        except: pass
                        self.sock = None
            if self.should_run:
                self._status("连接断开，3秒后重连...")
                time.sleep(3)

    def _status(self, text):
        sys.stdout.write(f"\r  状态: {text:<40}")
        sys.stdout.flush()

    def run(self):
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()
        try:
            while self.should_run:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n  正在退出...")
            self.should_run = False
            with self.lock:
                if self.sock:
                    try: self.sock.close()
                    except: pass

if __name__ == "__main__":
    DeployClient().run()
