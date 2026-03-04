"""
Codex Agent Loop
================
Uses OpenAI Responses API to iteratively execute shell commands
on a remote machine via the connected client.
"""

import asyncio
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Awaitable

import httpx

from config import CODEX_API_URL, CODEX_API_KEY, CODEX_MODEL, CLAUDE_API_URL, CLAUDE_API_KEY, CLAUDE_MODEL

logger = logging.getLogger("codex_agent")

MAX_ROUNDS = 20
CMD_TIMEOUT = 120  # seconds
HISTORY_TTL = 0  # 0 = never expire, conversation persists until manually cleared
MAX_HISTORY_MESSAGES = 100  # keep last 100 messages per device

SYSTEM_DIRECTIVE = """\
[SYSTEM DIRECTIVE - FOLLOW EXACTLY]
You are an expert remote system engineer with FULL SHELL ACCESS to a colleague's machine.
Target: OS={os}, Arch={arch}, Hostname={hostname}

Respond ONLY with valid JSON. No markdown, no fences, no extra text.

To run a command: {{"cmd": "shell command", "thinking": "detailed analysis and reason"}}
To reply/explain: {{"reply": "your message"}}
To finish task:   {{"done": true, "thinking": "summary of what was accomplished"}}

Rules:
- When asked to DO something on the machine, ALWAYS use cmd. NEVER just give advice.
- {shell_hint}
- One command at a time, wait for output before next.
- Use "thinking" to show your full reasoning — diagnose before acting, explain what you found.
- If you need more info before acting, run a diagnostic command first (e.g., check status, read config).
- If the request is truly ambiguous or risky, use "reply" to ask ONE clarifying question.
- ALWAYS reply in Chinese (简体中文). Never reply in English.
- If the user says something vague like "怎么样" or "继续", refer to history and continue.
- When downloading files with curl, ALWAYS add --no-progress-meter or -s flag to suppress progress. Example: curl -fsSL url -o file
- When using wget, ALWAYS add -q flag to suppress progress. Example: wget -q url -O file
- When using pip/npm/apt, they handle progress themselves; do not add extra flags.
[END DIRECTIVE]"""

# ---------------------------------------------------------------------------
# Per-device conversation history (keyed by hostname for persistence across reconnects)
# ---------------------------------------------------------------------------
# { hostname: {"messages": [...], "device_info": {...}, "updated_at": float} }
_device_history: dict[str, dict] = {}
# Mapping: device_code -> hostname (for lookup)
_code_to_hostname: dict[str, str] = {}

# ---------------------------------------------------------------------------
# httpx connection pool (reused across API calls)
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=30))
    return _http_client


async def close_http_client():
    """Close the shared httpx client. Called on server shutdown."""
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def _get_history(code: str, device_info: dict) -> list[dict]:
    """Get or create conversation history for a device.

    History is keyed by hostname so it persists across client reconnects
    (which generate new device codes).
    """
    hostname = device_info.get("hostname", code)
    _code_to_hostname[code] = hostname

    now = time.time()
    entry = _device_history.get(hostname)

    # Expire old history only if TTL is set
    if entry and HISTORY_TTL > 0 and (now - entry["updated_at"]) > HISTORY_TTL:
        logger.info("Conversation history expired for %s (%s), resetting", code, hostname)
        entry = None

    if entry is None:
        _device_history[hostname] = {
            "messages": [],
            "device_info": device_info,
            "updated_at": now,
        }
        return _device_history[hostname]["messages"]

    # Update device info in case OS/arch changed
    entry["device_info"] = device_info
    entry["updated_at"] = now
    messages = entry["messages"]

    # Trim to keep history bounded (preserve first message with system directive)
    if len(messages) > MAX_HISTORY_MESSAGES:
        first = messages[0]
        trimmed = [first] + messages[-(MAX_HISTORY_MESSAGES - 1):]
        entry["messages"] = trimmed
        messages = entry["messages"]
        logger.info("Trimmed conversation history for %s (%s) to %d messages", code, hostname, len(messages))

    return messages


def clear_history(code: str):
    """Clear conversation history for a device."""
    hostname = _code_to_hostname.get(code, code)
    _device_history.pop(hostname, None)
    _code_to_hostname.pop(code, None)
    logger.info("Cleared conversation history for %s (%s)", code, hostname)


def _build_directive(device_info: dict) -> str:
    os_name = device_info.get("os", "Unknown")
    shell_hint = "Use cmd.exe commands (NOT PowerShell)." if os_name == "Windows" else "Use bash commands."
    return SYSTEM_DIRECTIVE.format(
        os=os_name,
        arch=device_info.get("arch", "Unknown"),
        hostname=device_info.get("hostname", "Unknown"),
        shell_hint=shell_hint,
    )


# ---------------------------------------------------------------------------
# Skill injection - load skill files and inject into conversation
# ---------------------------------------------------------------------------

_SKILLS_DIR = Path(__file__).parent / "skills"

# Cache loaded skill content to avoid repeated disk reads
_skill_cache: dict[str, str] = {}


def _load_skill(name: str) -> str:
    """Load a skill file from the skills directory (cached)."""
    if name in _skill_cache:
        return _skill_cache[name]
    path = _SKILLS_DIR / f"{name}.md"
    try:
        content = path.read_text(encoding="utf-8")
        _skill_cache[name] = content
        logger.info("Loaded skill: %s (%d chars)", name, len(content))
        return content
    except Exception as e:
        logger.warning("Failed to load skill %s: %s", name, e)
        return ""


# OpenClaw keyword patterns (case-insensitive)
_OPENCLAW_KEYWORDS = re.compile(
    r"openclaw|open\s*claw|clawbot|moltbot|安装.*claw|部署.*claw|claw.*安装|claw.*部署",
    re.IGNORECASE,
)


def _detect_skill(instruction: str) -> str | None:
    """Detect which skill (if any) should be injected for this instruction."""
    if _OPENCLAW_KEYWORDS.search(instruction):
        return "openclaw"
    return None


async def process_instruction(
    instruction: str,
    code: str,
    device_info: dict,
    send_command: Callable[[str, str, str], Awaitable[None]],
    wait_for_output: Callable[[str], Awaitable[dict]],
    broadcast_to_admins: Callable[[dict], Awaitable[None]],
    model: str = "codex",
):
    directive = _build_directive(device_info)
    history = _get_history(code, device_info)

    try:
        await _run_instruction_loop(
            instruction, code, directive, history, send_command, wait_for_output, broadcast_to_admins, model
        )
    except asyncio.CancelledError:
        logger.info("Task cancelled for device %s", code)
        await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
        raise


async def _run_instruction_loop(
    instruction: str,
    code: str,
    directive: str,
    history: list,
    send_command: Callable[[str, str, str], Awaitable[None]],
    wait_for_output: Callable[[str], Awaitable[dict]],
    broadcast_to_admins: Callable[[dict], Awaitable[None]],
    model: str = "codex",
):
    # Build conversation for API call
    if not history:
        # First message: embed directive + skill (if applicable) + user request
        skill_name = _detect_skill(instruction)
        skill_content = ""
        if skill_name:
            skill_content = _load_skill(skill_name)
            if skill_content:
                logger.info("Injecting skill '%s' for device %s", skill_name, code)

        first_msg = directive
        if skill_content:
            first_msg += f"\n\n[SKILL REFERENCE - {skill_name.upper()}]\n{skill_content}\n[END SKILL]"
        first_msg += "\n\n[USER REQUEST]\n" + instruction
        history.append({"role": "user", "content": first_msg})
    else:
        # Follow-up: just add user message (directive already in history)
        history.append({"role": "user", "content": instruction})

    commands_executed = 0  # Track how many commands have been run

    for round_num in range(1, MAX_ROUNDS + 1):
        logger.info("Round %d/%d for device %s", round_num, MAX_ROUNDS, code)

        # --- Call API (with one retry on timeout) ---
        await broadcast_to_admins({"type": "status", "code": code, "state": "calling_api"})
        response_text = None
        for attempt in range(2):
            try:
                if model == "claude":
                    response_text = await _call_claude(history)
                else:
                    response_text = await _call_codex(history)
                break
            except httpx.TimeoutException as e:
                if attempt == 0:
                    logger.warning("Codex API timeout (attempt 1), retrying: %s", e)
                    await broadcast_to_admins({"type": "log", "code": code, "msg": "API timeout, retrying..."})
                    continue
                error_msg = f"Codex API timeout after retry: {e}"
                logger.error(error_msg)
                await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
                await broadcast_to_admins({"type": "error", "code": code, "msg": error_msg})
                return
            except Exception as e:
                error_msg = f"Codex API error: {e}"
                logger.error(error_msg)
                await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
                await broadcast_to_admins({"type": "error", "code": code, "msg": error_msg})
                return

        await broadcast_to_admins({"type": "status", "code": code, "state": "thinking"})

        logger.info("Codex response: %s", response_text[:300])

        # --- Parse response ---
        parsed = _parse_response(response_text)

        # If not valid JSON, treat as chat reply
        if parsed is None:
            logger.info("Non-JSON response, treating as chat reply")
            history.append({"role": "assistant", "content": response_text.strip()})
            await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
            await broadcast_to_admins({
                "type": "reply",
                "code": code,
                "text": response_text.strip(),
            })
            return

        # --- Handle reply ---
        reply_text = parsed.get("reply", "")
        if reply_text:
            history.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})

            # Nudge if NO commands executed yet (AI is just planning instead of acting)
            if commands_executed == 0 and round_num <= 3:
                await broadcast_to_admins({
                    "type": "codex",
                    "code": code,
                    "thinking": reply_text,
                })
                nudges = [
                    "不要只说计划，直接执行命令。现在就开始第一步。",
                    "请立即用 {\"cmd\": \"...\"} 格式执行命令，不要再描述。",
                    "这是最后一次提醒：必须返回 cmd 字段执行命令，否则任务将终止。",
                ]
                nudge = nudges[min(round_num - 1, len(nudges) - 1)]
                history.append({"role": "user", "content": nudge})
                continue

            # Nudge if commands were executed but AI replied instead of continuing/finishing
            if commands_executed > 0:
                await broadcast_to_admins({
                    "type": "codex",
                    "code": code,
                    "thinking": reply_text,
                })
                history.append({"role": "user", "content":
                    '请继续执行任务：如果全部完成了请返回 {"done": true, "thinking": "完成总结"}；'
                    '如果还有步骤未完成，请直接用 {"cmd": "..."} 执行下一个命令。'
                })
                continue

            await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
            await broadcast_to_admins({
                "type": "reply",
                "code": code,
                "text": reply_text,
            })
            return

        # --- Handle command ---
        thinking = parsed.get("thinking", "")
        cmd = parsed.get("cmd", "")
        done = parsed.get("done", False)

        if thinking:
            await broadcast_to_admins({
                "type": "codex",
                "code": code,
                "thinking": thinking,
            })

        if done and not cmd:
            history.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
            await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
            await broadcast_to_admins({
                "type": "log",
                "code": code,
                "msg": f"Task completed: {thinking or reply_text}",
            })
            return

        if not cmd:
            # No cmd and no reply - nudge only if no commands executed yet
            if commands_executed == 0 and round_num <= 3:
                history.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
                nudge = "你返回了空命令。请直接用 cmd 字段执行具体命令。"
                history.append({"role": "user", "content": nudge})
                continue

            await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
            await broadcast_to_admins({
                "type": "error",
                "code": code,
                "msg": "Codex returned empty response.",
            })
            return

        # --- Send to client ---
        task_id = str(uuid.uuid4())[:8]
        await broadcast_to_admins({"type": "status", "code": code, "state": "executing"})

        try:
            await send_command(code, task_id, cmd)
            commands_executed += 1
        except Exception as e:
            await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
            await broadcast_to_admins({"type": "error", "code": code, "msg": f"Send failed: {e}"})
            return

        # --- Wait for output ---
        try:
            result = await wait_for_output(task_id)
        except asyncio.TimeoutError:
            result = {"data": "[TIMEOUT] Command timed out.", "exit_code": -1}
            await broadcast_to_admins({"type": "log", "code": code, "msg": f"Command timed out ({CMD_TIMEOUT}s)"})

        output_text = result.get("data", "")
        exit_code = result.get("exit_code", -1)

        # Append to history
        history.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})

        output_summary = output_text
        if len(output_summary) > 8000:
            output_summary = output_summary[:4000] + "\n...[TRUNCATED]...\n" + output_summary[-4000:]

        history.append({
            "role": "user",
            "content": f"Command output (exit_code={exit_code}):\n{output_summary}",
        })

    await broadcast_to_admins({"type": "status", "code": code, "state": "idle"})
    await broadcast_to_admins({
        "type": "error",
        "code": code,
        "msg": f"Reached maximum {MAX_ROUNDS} rounds.",
    })


async def _call_codex(conversation: list[dict]) -> str:
    """Call the Responses API and return the assistant text."""
    if not CODEX_API_KEY:
        raise ValueError("CODEX_API_KEY environment variable not set")

    headers = {
        "Authorization": f"Bearer {CODEX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CODEX_MODEL,
        "input": conversation,
    }

    client = _get_http_client()
    resp = await client.post(CODEX_API_URL, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()

    output = data.get("output", [])
    for item in output:
        if item.get("type") == "message":
            content = item.get("content", [])
            for block in content:
                if block.get("type") == "output_text":
                    return block.get("text", "").strip()

    raise ValueError(f"No text in response: {json.dumps(data)[:300]}")


async def _call_claude(conversation: list[dict]) -> str:
    """Call Claude via cc.ioasis.xyz relay (OpenAI Chat Completions format)."""
    if not CLAUDE_API_KEY:
        raise ValueError("CLAUDE_API_KEY not set")

    headers = {
        "Authorization": f"Bearer {CLAUDE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "messages": conversation,
        "max_tokens": 4096,
    }

    client = _get_http_client()
    resp = await client.post(CLAUDE_API_URL, headers=headers, json=payload)
    resp.raise_for_status()
    data = resp.json()

    choices = data.get("choices", [])
    if choices:
        return choices[0].get("message", {}).get("content", "").strip()
    raise ValueError(f"No content in Claude response: {json.dumps(data)[:300]}")


def _parse_response(text: str) -> dict | None:
    """Parse JSON from response, handling markdown fences and Windows paths."""
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # Try direct parse first
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Extract JSON object substring
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = cleaned[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        # Fix unescaped backslashes (common with Windows paths like %USERPROFILE%\.openclaw)
        # Replace \ not followed by valid JSON escape chars with \\
        fixed = re.sub(r'\\(?!["\\/bfnrtu0-9])', r'\\\\', candidate)
        try:
            parsed = json.loads(fixed)
            if isinstance(parsed, dict):
                logger.debug("Parsed JSON after backslash fix")
                return parsed
        except json.JSONDecodeError:
            pass

    return None
