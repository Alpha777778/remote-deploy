"""
Remote Deploy Server
====================
FastAPI + WebSocket server that manages:
- Client connections (colleagues' machines)
- Admin connections (dashboard browser)
- Device registry (pairing code -> connection)
- Command dispatch (admin instruction -> Codex agent -> client)

Runs on port 5100.
"""

import asyncio
import collections
import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from fastapi import Request

from config import HOST, PORT, TOTP_SECRET
import codex_agent
import pyotp

# TOTP verifier (allows current code +/- 1 window for clock drift)
_totp = pyotp.TOTP(TOTP_SECRET)


def verify_totp(code: str) -> bool:
    """Verify a 6-digit TOTP code, allowing 1 window tolerance."""
    return _totp.verify(code, valid_window=1)

# ---------------------------------------------------------------------------
# Session token store (persisted to disk, survives server restarts, 24h TTL)
# ---------------------------------------------------------------------------
# { token_str: expiry_timestamp }
_session_tokens: dict[str, float] = {}
_SESSION_TTL = 86400  # 24 hours
_SESSION_FILE = Path(__file__).parent / "sessions.json"


def _load_sessions():
    """Load session tokens from disk."""
    global _session_tokens
    if _SESSION_FILE.exists():
        try:
            data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
            now = time.time()
            _session_tokens = {k: v for k, v in data.items() if v > now}
        except Exception:
            _session_tokens = {}


def _save_sessions():
    """Persist session tokens to disk."""
    try:
        now = time.time()
        valid = {k: v for k, v in _session_tokens.items() if v > now}
        _SESSION_FILE.write_text(json.dumps(valid), encoding="utf-8")
    except Exception:
        pass


_load_sessions()


def _create_session() -> str:
    """Generate a new session token."""
    token = secrets.token_urlsafe(32)
    _session_tokens[token] = time.time() + _SESSION_TTL
    _save_sessions()
    return token


def _verify_session(token: str) -> bool:
    """Check if a session token is valid and not expired."""
    expiry = _session_tokens.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        _session_tokens.pop(token, None)
        _save_sessions()
        return False
    return True

# ---------------------------------------------------------------------------
# Rate limiting for client registration (Fix 4)
# ---------------------------------------------------------------------------
# { ip: [timestamp, timestamp, ...] }
_reg_attempts: dict[str, list[float]] = {}
_REG_RATE_LIMIT = 10  # max registrations per window
_REG_RATE_WINDOW = 60  # seconds


def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is within rate limit, False if exceeded."""
    now = time.time()
    attempts = _reg_attempts.get(ip, [])
    # Remove expired entries
    attempts = [t for t in attempts if now - t < _REG_RATE_WINDOW]
    _reg_attempts[ip] = attempts
    if len(attempts) >= _REG_RATE_LIMIT:
        return False
    attempts.append(now)
    return True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("deploy_server")

# ---------------------------------------------------------------------------
# Log buffer for real-time log viewer (circular buffer, last 500 entries)
# ---------------------------------------------------------------------------
LOG_BUFFER_SIZE = 500
log_buffer: collections.deque[dict] = collections.deque(maxlen=LOG_BUFFER_SIZE)


# Loggers worth showing in admin panel
_ALLOWED_LOGGERS = {"deploy_server", "codex_agent", "uvicorn.error"}


class AdminLogHandler(logging.Handler):
    """Captures log records into a buffer and broadcasts to admin WS clients."""

    def emit(self, record: logging.LogRecord):
        try:
            # Filter out noisy/useless logs (heartbeat pongs, library internals)
            msg_text = record.getMessage()
            if msg_text.strip() in ("ok", "pong", ""):
                return
            if record.name not in _ALLOWED_LOGGERS and not record.name.startswith("deploy_"):
                return

            entry = {
                "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3],
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),
            }
            log_buffer.append(entry)
            # Schedule broadcast (non-blocking)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_broadcast_log_entry(entry))
            except RuntimeError:
                pass  # No running loop yet (startup)
        except Exception:
            pass


async def _broadcast_log_entry(entry: dict):
    """Send a single log entry to all admin connections (concurrent)."""
    if not admin_connections:
        return
    msg = json.dumps({"type": "server_log", "entry": entry}, ensure_ascii=False)

    async def _send(ws):
        try:
            await ws.send_text(msg)
            return None
        except Exception:
            return ws

    results = await asyncio.gather(*[_send(ws) for ws in admin_connections])
    for ws in results:
        if ws is not None and ws in admin_connections:
            admin_connections.remove(ws)


# Install the handler on root logger to capture everything
_admin_handler = AdminLogHandler()
_admin_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
_admin_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_admin_handler)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Remote Deploy Server", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"

# Mount static files only if the directory exists and has content
if STATIC_DIR.exists():
    app.mount("/deploy/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# Device registry: code -> device info dict
# {
#   "code": str,
#   "os": str,
#   "arch": str,
#   "hostname": str,
#   "status": "online" | "offline",
#   "connected_at": float,
#   "ws": WebSocket (not serialized),
# }
devices: dict[str, dict[str, Any]] = {}

# Admin WebSocket connections
admin_connections: list[WebSocket] = []

# Running agent tasks: device_code -> asyncio.Task (for cancellation)
_running_agent_tasks: dict[str, asyncio.Task] = {}

# Per-device message buffer: keeps recent messages so reconnecting admins
# can catch up on what they missed. Keyed by device code.
# { device_code: deque([{...msg with _seq...}, ...]) }
_device_msg_buffer: dict[str, collections.deque] = {}
_DEVICE_BUF_SIZE = 50  # messages per device
_msg_seq = 0  # global sequence counter


def _buffer_msg(message: dict):
    """Buffer a device-related message with a sequence number."""
    global _msg_seq
    code = message.get("code")
    if not code:
        return
    _msg_seq += 1
    message["_seq"] = _msg_seq
    if code not in _device_msg_buffer:
        _device_msg_buffer[code] = collections.deque(maxlen=_DEVICE_BUF_SIZE)
    _device_msg_buffer[code].append(message)

# Task output tracking: task_id -> asyncio.Event + result storage
# Each entry: {"event": asyncio.Event, "data": str, "exit_code": int, "chunks": [str]}
pending_tasks: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def device_list_payload() -> dict:
    """Build the 'devices' message for admins."""
    return {
        "type": "devices",
        "list": [
            {
                "code": info["code"],
                "os": info.get("os", "Unknown"),
                "arch": info.get("arch", "Unknown"),
                "hostname": info.get("hostname", "Unknown"),
                "status": info.get("status", "offline"),
            }
            for info in devices.values()
        ],
    }


async def broadcast_to_admins(message: dict):
    """Send a JSON message to all connected admin WebSockets (concurrent).
    Always buffers important device messages for replay on reconnect."""
    # Always buffer important messages with a device code
    msg_type = message.get("type", "")
    if msg_type in ("reply", "output", "error", "codex", "log", "status"):
        _buffer_msg(message)

    if not admin_connections:
        return
    text = json.dumps(message, ensure_ascii=False)

    async def _send(ws):
        try:
            await ws.send_text(text)
            return None
        except Exception:
            return ws

    results = await asyncio.gather(*[_send(ws) for ws in admin_connections])
    for ws in results:
        if ws is not None and ws in admin_connections:
            admin_connections.remove(ws)


async def send_command_to_client(code: str, task_id: str, cmd: str):
    """Send an exec command to a connected client device."""
    device = devices.get(code)
    if not device:
        raise ValueError(f"Device {code} not found")
    if device.get("status") != "online":
        raise ValueError(f"Device {code} is offline")

    ws = device.get("ws")
    if ws is None:
        raise ValueError(f"Device {code} has no WebSocket connection")

    # Prepare task output tracking
    pending_tasks[task_id] = {
        "event": asyncio.Event(),
        "data": "",
        "exit_code": -1,
        "chunks": [],
        "created_at": time.time(),
    }

    payload = json.dumps({"type": "exec", "task_id": task_id, "cmd": cmd})
    try:
        await ws.send_text(payload)
    except Exception:
        pending_tasks.pop(task_id, None)
        raise

    # Also notify admins about the command being sent
    await broadcast_to_admins({
        "type": "output",
        "code": code,
        "task_id": task_id,
        "data": f"$ {cmd}\n",
    })


async def wait_for_task_output(task_id: str) -> dict:
    """Wait for a task to complete and return its output."""
    task_info = pending_tasks.get(task_id)
    if task_info is None:
        raise ValueError(f"Task {task_id} not found")

    try:
        await asyncio.wait_for(
            task_info["event"].wait(),
            timeout=codex_agent.CMD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        # Clean up and re-raise
        pending_tasks.pop(task_id, None)
        raise

    result = {
        "data": task_info["data"],
        "exit_code": task_info["exit_code"],
    }
    pending_tasks.pop(task_id, None)
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/deploy/")
async def admin_dashboard():
    """Serve the admin dashboard HTML."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    # Placeholder when dashboard has not been created yet
    return HTMLResponse(
        content=(
            "<html><body>"
            "<h1>Remote Deploy - Admin Dashboard</h1>"
            "<p>Dashboard HTML not yet created. Place <code>index.html</code> in "
            "<code>server/static/</code>.</p>"
            "</body></html>"
        )
    )


@app.get("/deploy/api/devices")
async def api_devices(request: Request, token: str = ""):
    """REST endpoint to list connected devices (requires TOTP code)."""
    auth_header = request.headers.get("authorization", "")
    provided = token or (auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else "")
    if not verify_totp(provided):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return JSONResponse(content=device_list_payload())


# ---------------------------------------------------------------------------
# WebSocket: Client endpoint
# ---------------------------------------------------------------------------

@app.websocket("/deploy/ws/client")
async def ws_client(ws: WebSocket):
    await ws.accept()
    code = None
    client_ip = ws.client.host if ws.client else "unknown"
    logger.info("Client WebSocket connected from %s (awaiting registration)", client_ip)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=120)
            except asyncio.TimeoutError:
                # No message in 120s, check if client is alive
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Client sent invalid JSON: %s", raw[:200])
                continue

            msg_type = msg.get("type")

            # --- Registration ---
            if msg_type == "register":
                # Rate limit check
                if not _check_rate_limit(client_ip):
                    logger.warning("Rate limit exceeded for %s", client_ip)
                    await ws.send_text(json.dumps({"type": "error", "msg": "Rate limit exceeded"}))
                    continue

                code = msg.get("code", "").strip().upper()
                if not code:
                    await ws.send_text(json.dumps({"type": "error", "msg": "Missing pairing code"}))
                    continue

                hostname = msg.get("hostname", "Unknown")

                # If same code already exists (reconnection), just update the WS
                if code in devices:
                    old_ws = devices[code].get("ws")
                    if old_ws and old_ws != ws:
                        try:
                            await old_ws.close()
                        except Exception:
                            pass
                    devices[code]["ws"] = ws
                    devices[code]["status"] = "online"
                    devices[code]["connected_at"] = time.time()
                    logger.info("Client reconnected: code=%s hostname=%s", code, hostname)
                else:
                    # Remove stale entries from same hostname (different code)
                    # NOTE: Do NOT close old WS connections - that triggers reconnect
                    # loops. Just remove them from the device list silently.
                    stale_codes = [
                        c for c, info in devices.items()
                        if c != code
                        and info.get("hostname") == hostname
                    ]
                    for sc in stale_codes:
                        logger.info("Removing stale device %s (same hostname %s, keeping WS open)", sc, hostname)
                        del devices[sc]

                    devices[code] = {
                        "code": code,
                        "os": msg.get("os", "Unknown"),
                        "arch": msg.get("arch", "Unknown"),
                        "hostname": hostname,
                        "status": "online",
                        "connected_at": time.time(),
                        "ws": ws,
                    }
                    logger.info(
                        "Client registered: code=%s os=%s hostname=%s",
                        code, msg.get("os"), msg.get("hostname"),
                    )

                # Acknowledge registration
                await ws.send_text(json.dumps({"type": "registered", "code": code}))

                # Notify admins
                await broadcast_to_admins({
                    "type": "log",
                    "code": code,
                    "msg": f"Client {code} connected ({msg.get('os', '?')} / {msg.get('hostname', '?')})",
                })
                await broadcast_to_admins(device_list_payload())

            # --- Command output ---
            elif msg_type == "output":
                task_id = msg.get("task_id")
                data = msg.get("data", "")
                done = msg.get("done", False)
                exit_code = msg.get("exit_code", None)

                if task_id and task_id in pending_tasks:
                    task_info = pending_tasks[task_id]
                    if data:
                        task_info["chunks"].append(data)

                    if done:
                        task_info["data"] = "".join(task_info["chunks"])
                        task_info["exit_code"] = exit_code if exit_code is not None else 0
                        task_info["event"].set()

                # Stream output to admins in real time
                if code and data:
                    await broadcast_to_admins({
                        "type": "output",
                        "code": code,
                        "task_id": task_id,
                        "data": data,
                    })

                if done and code:
                    await broadcast_to_admins({
                        "type": "log",
                        "code": code,
                        "msg": f"Command completed (exit_code={exit_code})",
                    })

            # --- Heartbeat / ping ---
            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

            # --- Heartbeat / pong (response to server's ping) ---
            elif msg_type == "pong":
                pass  # client is alive, nothing to do

            else:
                logger.warning("Unknown client message type: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("Client WebSocket disconnected: code=%s", code)
    except Exception as e:
        logger.error("Client WebSocket error: %s", e)
    finally:
        # Mark device offline
        if code and code in devices:
            devices[code]["status"] = "offline"
            devices[code].pop("ws", None)
            await broadcast_to_admins({
                "type": "log",
                "code": code,
                "msg": f"Client {code} disconnected",
            })
            await broadcast_to_admins(device_list_payload())


# ---------------------------------------------------------------------------
# WebSocket: Admin endpoint
# ---------------------------------------------------------------------------

@app.websocket("/deploy/ws/admin")
async def ws_admin(ws: WebSocket):
    await ws.accept()
    logger.info("Admin WebSocket connected (awaiting auth)")

    # --- Auth handshake: TOTP code or session token ---
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") != "auth":
            await ws.send_text(json.dumps({"type": "auth_failed", "msg": "Invalid auth message"}))
            await ws.close()
            return
        session = msg.get("session", "")
        totp_code = str(msg.get("token", ""))
        if session and _verify_session(session):
            # Session token auth (reconnect)
            session_token = session
            logger.info("Admin authenticated via session token")
        elif totp_code and verify_totp(totp_code):
            # TOTP auth (first login), issue new session
            session_token = _create_session()
            logger.info("Admin authenticated via TOTP, session issued")
        else:
            await ws.send_text(json.dumps({"type": "auth_failed", "msg": "验证码无效或已过期"}))
            await ws.close()
            logger.warning("Admin auth failed (invalid TOTP/session)")
            return
    except (asyncio.TimeoutError, json.JSONDecodeError, Exception):
        try:
            await ws.send_text(json.dumps({"type": "auth_failed", "msg": "Auth timeout"}))
            await ws.close()
        except Exception:
            pass
        logger.warning("Admin auth timeout or error")
        return

    await ws.send_text(json.dumps({"type": "auth_ok", "session": session_token}))
    admin_connections.append(ws)
    logger.info("Admin authenticated (total: %d)", len(admin_connections))

    # Send current device list immediately
    try:
        await ws.send_text(json.dumps(device_list_payload()))
    except Exception:
        if ws in admin_connections:
            admin_connections.remove(ws)
        return

    # Send buffered logs so admin sees recent history
    try:
        await ws.send_text(json.dumps({
            "type": "server_logs_bulk",
            "entries": list(log_buffer),
        }, ensure_ascii=False))
    except Exception:
        pass

    # Replay per-device message buffer so admin catches up on missed messages
    # The frontend deduplicates by _seq
    last_seq = msg.get("last_seq", 0)  # from auth message
    replay_count = 0
    for code_key, buf in _device_msg_buffer.items():
        for buffered_msg in buf:
            if buffered_msg.get("_seq", 0) <= last_seq:
                continue
            try:
                await ws.send_text(json.dumps(buffered_msg, ensure_ascii=False))
                replay_count += 1
            except Exception:
                break
    if replay_count:
        logger.info("Replayed %d buffered device messages to admin (after seq %d)", replay_count, last_seq)

    try:
        while True:
            # Use wait_for to implement a server-side ping timeout
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                # No message in 60s, send a ping to check if alive
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Admin sent invalid JSON: %s", raw[:200])
                continue

            msg_type = msg.get("type")

            # --- Instruction to execute on a device ---
            if msg_type == "instruction":
                target_code = msg.get("code", "").strip().upper()
                text = msg.get("text", "").strip()

                logger.info("Instruction from admin for %s: %s", target_code, text[:100])

                if not target_code or not text:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "code": target_code,
                        "msg": "Missing code or instruction text",
                    }))
                    continue

                device = devices.get(target_code)
                if not device:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "code": target_code,
                        "msg": f"Device {target_code} not found",
                    }))
                    continue

                if device.get("status") != "online":
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "code": target_code,
                        "msg": f"Device {target_code} is offline",
                    }))
                    continue

                # Launch agent loop as a background task
                device_info = {
                    "os": device.get("os", "Unknown"),
                    "arch": device.get("arch", "Unknown"),
                    "hostname": device.get("hostname", "Unknown"),
                }
                model = msg.get("model", "codex")

                await broadcast_to_admins({
                    "type": "log",
                    "code": target_code,
                    "msg": f"Instruction received [{model}]: {text}",
                })

                # Track the task so admin can cancel it
                task = asyncio.create_task(
                    codex_agent.process_instruction(
                        instruction=text,
                        code=target_code,
                        device_info=device_info,
                        send_command=send_command_to_client,
                        wait_for_output=wait_for_task_output,
                        broadcast_to_admins=broadcast_to_admins,
                        model=model,
                    )
                )
                _running_agent_tasks[target_code] = task
                task.add_done_callback(lambda t, c=target_code: _running_agent_tasks.pop(c, None))

            # --- Cancel running task ---
            elif msg_type == "cancel":
                target_code = msg.get("code", "").strip().upper()
                task = _running_agent_tasks.get(target_code)
                if task and not task.done():
                    task.cancel()
                    await broadcast_to_admins({"type": "status", "code": target_code, "state": "idle"})
                    await broadcast_to_admins({"type": "log", "code": target_code, "msg": "Task cancelled by admin"})
                    await broadcast_to_admins({"type": "reply", "code": target_code, "text": "⛔ 任务已中断"})
                else:
                    await ws.send_text(json.dumps({"type": "log", "code": target_code, "msg": "No running task to cancel"}))

            # --- Ping ---
            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

            else:
                logger.warning("Unknown admin message type: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("Admin WebSocket disconnected")
    except Exception as e:
        logger.error("Admin WebSocket error: %s", e)
    finally:
        if ws in admin_connections:
            admin_connections.remove(ws)
        logger.info("Admin connections remaining: %d", len(admin_connections))


# ---------------------------------------------------------------------------
# Startup / Shutdown events
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    logger.info("Remote Deploy Server starting on %s:%d", HOST, PORT)
    logger.info("Admin dashboard: http://localhost:%d/deploy/", PORT)
    logger.info("Client WS:      ws://localhost:%d/deploy/ws/client", PORT)
    logger.info("Admin WS:       ws://localhost:%d/deploy/ws/admin", PORT)
    # Start periodic stale task cleanup
    asyncio.create_task(_cleanup_stale_tasks())


async def _cleanup_stale_tasks():
    """Periodically remove stale pending tasks (older than 5 min)."""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        stale = [tid for tid, info in pending_tasks.items()
                 if now - info.get("created_at", now) > 300]
        for tid in stale:
            pending_tasks.pop(tid, None)
        if stale:
            logger.info("Cleaned up %d stale pending tasks", len(stale))


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Remote Deploy Server shutting down")
    # Close httpx connection pool
    await codex_agent.close_http_client()
    # Close all client connections gracefully
    for info in devices.values():
        ws = info.get("ws")
        if ws:
            try:
                await ws.close()
            except Exception:
                pass
    # Close all admin connections
    for ws in admin_connections:
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Direct run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
