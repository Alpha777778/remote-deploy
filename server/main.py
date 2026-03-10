"""
Remote Deploy Server - Multi-Room Edition
==========================================
Rooms: main / ren / cheng  — each fully isolated:
  - Separate device registry
  - Separate admin WebSocket connections
  - Separate message buffers
  - Separate TOTP auth
"""

import asyncio
import collections
import json
import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi import Request

from config import HOST, PORT, ROOM_TOTP_SECRETS
import codex_agent
import pyotp

# ---------------------------------------------------------------------------
# Room configuration
# ---------------------------------------------------------------------------

VALID_ROOMS = set(ROOM_TOTP_SECRETS.keys())   # {"main", "ren", "cheng"}

_room_totp = {room: pyotp.TOTP(secret) for room, secret in ROOM_TOTP_SECRETS.items()}


def verify_totp(code: str, room: str) -> bool:
    # Try the room's own TOTP first
    totp = _room_totp.get(room)
    if totp and totp.verify(code, valid_window=1):
        return True
    # Main room TOTP works as master key for any sub-room
    if room != "main":
        main_totp = _room_totp.get("main")
        if main_totp and main_totp.verify(code, valid_window=1):
            return True
    return False


# ---------------------------------------------------------------------------
# Session token store (room-scoped, survives restarts)
# ---------------------------------------------------------------------------
_session_tokens: dict[str, dict] = {}   # token -> {"expiry": float, "room": str}
_SESSION_TTL = 86400
_SESSION_FILE = Path(__file__).parent / "sessions.json"


def _load_sessions():
    global _session_tokens
    if _SESSION_FILE.exists():
        try:
            data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
            now = time.time()
            new_data = {}
            for k, v in data.items():
                if isinstance(v, (int, float)) and v > now:
                    new_data[k] = {"expiry": v, "room": "main"}   # migrate old format
                elif isinstance(v, dict) and v.get("expiry", 0) > now:
                    new_data[k] = v
            _session_tokens = new_data
        except Exception:
            _session_tokens = {}


def _save_sessions():
    try:
        now = time.time()
        valid = {k: v for k, v in _session_tokens.items() if v.get("expiry", 0) > now}
        _SESSION_FILE.write_text(json.dumps(valid), encoding="utf-8")
    except Exception:
        pass


_load_sessions()


def _create_session(room: str) -> str:
    token = secrets.token_urlsafe(32)
    _session_tokens[token] = {"expiry": time.time() + _SESSION_TTL, "room": room}
    _save_sessions()
    return token


def _verify_session(token: str, room: str) -> bool:
    entry = _session_tokens.get(token)
    if not entry:
        return False
    if time.time() > entry.get("expiry", 0):
        _session_tokens.pop(token, None)
        _save_sessions()
        return False
    token_room = entry.get("room")
    # Exact match, or main-room session acts as master for any sub-room
    return token_room == room or (token_room == "main" and room != "main")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
_reg_attempts: dict[str, list[float]] = {}
_REG_RATE_LIMIT = 10
_REG_RATE_WINDOW = 60


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _reg_attempts.get(ip, []) if now - t < _REG_RATE_WINDOW]
    _reg_attempts[ip] = attempts
    if len(attempts) >= _REG_RATE_LIMIT:
        return False
    attempts.append(now)
    return True


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("deploy_server")

LOG_BUFFER_SIZE = 50
log_buffer: collections.deque[dict] = collections.deque(maxlen=LOG_BUFFER_SIZE)

_ALLOWED_LOGGERS = {"deploy_server", "codex_agent", "uvicorn.error"}


class AdminLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
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
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_broadcast_log_all_rooms(entry))
            except RuntimeError:
                pass
        except Exception:
            pass


async def _broadcast_log_all_rooms(entry: dict):
    """Broadcast server log to all rooms' admins."""
    msg = json.dumps({"type": "server_log", "entry": entry}, ensure_ascii=False)
    for rs in _rooms.values():
        dead = set()
        for ws in list(rs.admin_connections):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        rs.admin_connections -= dead


_admin_handler = AdminLogHandler()
_admin_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
_admin_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_admin_handler)


# ---------------------------------------------------------------------------
# Per-room state
# ---------------------------------------------------------------------------

class RoomState:
    def __init__(self, name: str):
        self.name = name
        self.devices: dict[str, dict[str, Any]] = {}
        self.admin_connections: set[WebSocket] = set()
        self.device_msg_buffer: dict[str, collections.deque] = {}
        self.msg_seq: int = 0
        self.agent_tasks: dict[str, asyncio.Task] = {}

    def buffer_msg(self, message: dict):
        code = message.get("code")
        if not code:
            return
        self.msg_seq += 1
        message["_seq"] = self.msg_seq
        if code not in self.device_msg_buffer:
            self.device_msg_buffer[code] = collections.deque(maxlen=50)
        self.device_msg_buffer[code].append(message)

    def device_list_payload(self) -> dict:
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
                for info in self.devices.values()
                if info.get("status") == "online"
            ],
        }

    async def broadcast(self, message: dict):
        msg_type = message.get("type", "")
        if msg_type in ("reply", "output", "error", "codex", "log", "status"):
            self.buffer_msg(message)
        if not self.admin_connections:
            return
        text = json.dumps(message, ensure_ascii=False)
        dead = set()
        for ws in list(self.admin_connections):
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        self.admin_connections -= dead


_rooms: dict[str, RoomState] = {r: RoomState(r) for r in VALID_ROOMS}

# Global pending tasks (UUID task_ids, no collision across rooms)
pending_tasks: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# App & static files
# ---------------------------------------------------------------------------

app = FastAPI(title="Remote Deploy Server", version="2.0.0")
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/deploy/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _serve_html() -> HTMLResponse:
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(content=index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<html><body><h1>Remote Deploy</h1><p>index.html not found</p></body></html>")


@app.get("/deploy/")
async def admin_main():
    return _serve_html()


@app.get("/deploy/{room}/")
async def admin_room(room: str):
    if room not in VALID_ROOMS:
        return JSONResponse(status_code=404, content={"error": f"Unknown room: {room}"})
    return _serve_html()


@app.get("/deploy/api/devices")
async def api_devices(request: Request, token: str = "", room: str = "main"):
    auth_header = request.headers.get("authorization", "")
    provided = token or (auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else "")
    if not verify_totp(provided, room):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    rs = _rooms.get(room)
    return JSONResponse(content=rs.device_list_payload() if rs else {"type": "devices", "list": []})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def send_command_to_client(room: str, code: str, task_id: str, cmd: str):
    rs = _rooms[room]
    device = rs.devices.get(code)
    if not device:
        raise ValueError(f"Device {code} not found in room {room}")
    if device.get("status") != "online":
        raise ValueError(f"Device {code} is offline")
    ws = device.get("ws")
    if ws is None:
        raise ValueError(f"Device {code} has no WebSocket")

    pending_tasks[task_id] = {
        "event": asyncio.Event(),
        "data": "",
        "exit_code": -1,
        "chunks": [],
        "created_at": time.time(),
    }
    try:
        await ws.send_text(json.dumps({"type": "exec", "task_id": task_id, "cmd": cmd}))
    except Exception:
        pending_tasks.pop(task_id, None)
        raise

    await rs.broadcast({"type": "output", "code": code, "task_id": task_id, "data": f"$ {cmd}\n"})


async def wait_for_task_output(task_id: str) -> dict:
    task_info = pending_tasks.get(task_id)
    if task_info is None:
        raise ValueError(f"Task {task_id} not found")
    try:
        await asyncio.wait_for(task_info["event"].wait(), timeout=codex_agent.CMD_TIMEOUT)
    except asyncio.TimeoutError:
        pending_tasks.pop(task_id, None)
        raise
    result = {"data": task_info["data"], "exit_code": task_info["exit_code"]}
    pending_tasks.pop(task_id, None)
    return result


# ---------------------------------------------------------------------------
# WebSocket: Client
# ---------------------------------------------------------------------------

@app.websocket("/deploy/ws/client")
async def ws_client_legacy(websocket: WebSocket):
    """Backward compatibility: old clients without room → main room."""
    await ws_client(websocket, "main")


@app.websocket("/deploy/ws/client/{room}")
async def ws_client(ws: WebSocket, room: str):
    if room not in VALID_ROOMS:
        await ws.close()
        return

    await ws.accept()
    rs = _rooms[room]
    code = None
    client_ip = ws.client.host if ws.client else "unknown"
    logger.info("Client WS connected: room=%s ip=%s", room, client_ip)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=120)
            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "register":
                if not _check_rate_limit(client_ip):
                    await ws.send_text(json.dumps({"type": "error", "msg": "Rate limit exceeded"}))
                    continue
                code = msg.get("code", "").strip().upper()
                if not code:
                    await ws.send_text(json.dumps({"type": "error", "msg": "Missing pairing code"}))
                    continue
                hostname = msg.get("hostname", "Unknown")

                if code in rs.devices:
                    old_ws = rs.devices[code].get("ws")
                    if old_ws and old_ws != ws:
                        try:
                            await old_ws.close()
                        except Exception:
                            pass
                    rs.devices[code].update({"ws": ws, "status": "online", "connected_at": time.time()})
                    logger.info("Client reconnected: room=%s code=%s", room, code)
                else:
                    for sc in [c for c, i in rs.devices.items() if c != code and i.get("hostname") == hostname]:
                        del rs.devices[sc]
                    rs.devices[code] = {
                        "code": code,
                        "os": msg.get("os", "Unknown"),
                        "arch": msg.get("arch", "Unknown"),
                        "hostname": hostname,
                        "status": "online",
                        "connected_at": time.time(),
                        "ws": ws,
                    }
                    logger.info("Client registered: room=%s code=%s hostname=%s", room, code, hostname)

                await ws.send_text(json.dumps({"type": "registered", "code": code}))
                await rs.broadcast({
                    "type": "log", "code": code,
                    "msg": f"Client {code} connected ({msg.get('os', '?')} / {msg.get('hostname', '?')})",
                })
                await rs.broadcast(rs.device_list_payload())

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

                if code and data and not re.match(r'^\s*\d+\s+[\d.]+[kKmMgG]\s+\d+', data):
                    await rs.broadcast({"type": "output", "code": code, "task_id": task_id, "data": data})
                if done and code:
                    await rs.broadcast({"type": "log", "code": code, "msg": f"Command completed (exit_code={exit_code})"})

            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
            elif msg_type == "pong":
                pass

    except WebSocketDisconnect:
        logger.info("Client WS disconnected: room=%s code=%s", room, code)
    except Exception as e:
        logger.error("Client WS error: room=%s %s", room, e)
    finally:
        if code and code in rs.devices:
            rs.devices[code]["status"] = "offline"
            rs.devices[code].pop("ws", None)
            await rs.broadcast({"type": "log", "code": code, "msg": f"Client {code} disconnected"})
            await rs.broadcast(rs.device_list_payload())


# ---------------------------------------------------------------------------
# WebSocket: Admin
# ---------------------------------------------------------------------------

@app.websocket("/deploy/ws/admin/{room}")
async def ws_admin(ws: WebSocket, room: str):
    if room not in VALID_ROOMS:
        await ws.close()
        return

    await ws.accept()
    rs = _rooms[room]
    logger.info("Admin WS connected: room=%s (awaiting auth)", room)

    # --- Auth handshake ---
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") != "auth":
            await ws.send_text(json.dumps({"type": "auth_failed", "msg": "Invalid auth message"}))
            await ws.close()
            return
        session = msg.get("session", "")
        totp_code = str(msg.get("token", ""))
        if session and _verify_session(session, room):
            session_token = session
            logger.info("Admin auth via session: room=%s", room)
        elif totp_code and verify_totp(totp_code, room):
            session_token = _create_session(room)
            logger.info("Admin auth via TOTP: room=%s", room)
        else:
            await ws.send_text(json.dumps({"type": "auth_failed", "msg": "验证码无效或已过期"}))
            await ws.close()
            logger.warning("Admin auth FAILED: room=%s", room)
            return
    except Exception:
        try:
            await ws.send_text(json.dumps({"type": "auth_failed", "msg": "Auth timeout"}))
            await ws.close()
        except Exception:
            pass
        return

    await ws.send_text(json.dumps({"type": "auth_ok", "session": session_token}))
    rs.admin_connections.add(ws)
    logger.info("Admin authenticated: room=%s (total: %d)", room, len(rs.admin_connections))

    try:
        await ws.send_text(json.dumps(rs.device_list_payload()))
    except Exception:
        rs.admin_connections.discard(ws)
        return

    # Send buffered server logs
    try:
        await ws.send_text(json.dumps({"type": "server_logs_bulk", "entries": list(log_buffer)}, ensure_ascii=False))
    except Exception:
        pass

    # Replay device message buffer
    last_seq = msg.get("last_seq", 0)
    replay_count = 0
    for buf in rs.device_msg_buffer.values():
        for bm in buf:
            if bm.get("_seq", 0) <= last_seq:
                continue
            try:
                await ws.send_text(json.dumps(bm, ensure_ascii=False))
                replay_count += 1
            except Exception:
                break
    if replay_count:
        logger.info("Replayed %d buffered msgs to admin: room=%s", replay_count, room)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "instruction":
                target_code = msg.get("code", "").strip().upper()
                text = msg.get("text", "").strip()
                if not target_code or not text:
                    await ws.send_text(json.dumps({"type": "error", "code": target_code, "msg": "Missing code or text"}))
                    continue

                model = msg.get("model", "codex")

                # --- AI Assistant mode (no device needed) ---
                if target_code == codex_agent.AI_CODE:
                    task = asyncio.create_task(
                        codex_agent.process_chat(
                            instruction=text,
                            room=room,
                            broadcast_to_admins=rs.broadcast,
                            model=model,
                        )
                    )
                    rs.agent_tasks[codex_agent.AI_CODE] = task
                    task.add_done_callback(lambda t: rs.agent_tasks.pop(codex_agent.AI_CODE, None))
                    continue

                device = rs.devices.get(target_code)
                if not device:
                    await ws.send_text(json.dumps({"type": "error", "code": target_code, "msg": f"Device {target_code} not found"}))
                    continue
                if device.get("status") != "online":
                    await ws.send_text(json.dumps({"type": "error", "code": target_code, "msg": f"Device {target_code} is offline"}))
                    continue

                device_info = {"os": device.get("os", "Unknown"), "arch": device.get("arch", "Unknown"), "hostname": device.get("hostname", "Unknown")}
                await rs.broadcast({"type": "log", "code": target_code, "msg": f"Instruction received [{model}]: {text}"})

                async def _send_cmd(code, task_id, cmd, _room=room):
                    await send_command_to_client(_room, code, task_id, cmd)

                task = asyncio.create_task(
                    codex_agent.process_instruction(
                        instruction=text, code=target_code, device_info=device_info,
                        send_command=_send_cmd, wait_for_output=wait_for_task_output,
                        broadcast_to_admins=rs.broadcast, model=model,
                    )
                )
                rs.agent_tasks[target_code] = task
                task.add_done_callback(lambda t, c=target_code: rs.agent_tasks.pop(c, None))

            elif msg_type == "cancel":
                target_code = msg.get("code", "").strip().upper()
                task = rs.agent_tasks.get(target_code)
                if task and not task.done():
                    task.cancel()
                    await rs.broadcast({"type": "status", "code": target_code, "state": "idle"})
                    await rs.broadcast({"type": "log", "code": target_code, "msg": "Task cancelled by admin"})
                    await rs.broadcast({"type": "reply", "code": target_code, "text": "⛔ 任务已中断"})
                else:
                    await ws.send_text(json.dumps({"type": "log", "code": target_code, "msg": "No running task to cancel"}))

            elif msg_type == "new_session":
                target_code = msg.get("code", "").strip().upper()
                task = rs.agent_tasks.get(target_code)
                if task and not task.done():
                    task.cancel()
                if target_code == codex_agent.AI_CODE:
                    codex_agent.clear_ai_chat_history(room)
                    logger.info("AI chat history cleared: room=%s", room)
                    await ws.send_text(json.dumps({"type": "log", "code": target_code, "msg": "AI 助手记忆已清除，开始新对话"}))
                else:
                    codex_agent.clear_history(target_code)
                    logger.info("New session started: room=%s code=%s", room, target_code)
                    await ws.send_text(json.dumps({"type": "log", "code": target_code, "msg": "New session started, AI memory cleared"}))

            elif msg_type == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))

    except WebSocketDisconnect:
        logger.info("Admin WS disconnected: room=%s", room)
    except Exception as e:
        logger.error("Admin WS error: room=%s %s", room, e)
    finally:
        rs.admin_connections.discard(ws)
        logger.info("Admin connections: room=%s remaining=%d", room, len(rs.admin_connections))


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    logger.info("Remote Deploy Server v2 started on %s:%d rooms=%s", HOST, PORT, list(VALID_ROOMS))
    asyncio.create_task(_cleanup_stale_tasks())


async def _cleanup_stale_tasks():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        stale = [tid for tid, info in pending_tasks.items() if now - info.get("created_at", now) > 300]
        for tid in stale:
            pending_tasks.pop(tid, None)
        if stale:
            logger.info("Cleaned up %d stale pending tasks", len(stale))


@app.on_event("shutdown")
async def on_shutdown():
    logger.info("Remote Deploy Server shutting down")
    await codex_agent.close_http_client()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
