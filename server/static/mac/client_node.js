#!/usr/bin/env node
/**
 * Remote Deploy Client - 纯 Node.js 标准库版，无需任何 npm 包
 * Node.js 12+ 即可运行
 */
"use strict";
const net = require("net");
const os = require("os");
const crypto = require("crypto");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

const SERVER_HOST = "120.27.152.51";
const SERVER_PORT = 5100;
const SERVER_PATH = "/deploy/ws/client";

const CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
const CODE_FILE  = path.join(os.homedir(), ".remote_deploy", ".deploy_code");

// ── 配对码 ──────────────────────────────────────────────────────────────────

function generateCode() {
    try {
        const saved = fs.readFileSync(CODE_FILE, "utf8").trim();
        if (saved.length >= 4 && [...saved].every(c => CODE_CHARS.includes(c))) return saved;
    } catch (_) {}
    const code = Array.from({ length: 4 }, () => CODE_CHARS[Math.floor(Math.random() * CODE_CHARS.length)]).join("");
    try {
        fs.mkdirSync(path.dirname(CODE_FILE), { recursive: true });
        fs.writeFileSync(CODE_FILE, code);
    } catch (_) {}
    return code;
}

// ── WebSocket 帧编解码 ────────────────────────────────────────────────────────

function wsEncode(text) {
    const data   = Buffer.from(text, "utf8");
    const mask   = crypto.randomBytes(4);
    const masked = Buffer.alloc(data.length);
    for (let i = 0; i < data.length; i++) masked[i] = data[i] ^ mask[i % 4];
    const n = data.length;
    let hdr;
    if (n < 126)        hdr = Buffer.from([0x81, 0x80 | n]);
    else if (n < 65536) hdr = Buffer.from([0x81, 0xFE, n >> 8, n & 0xFF]);
    else {
        hdr = Buffer.alloc(10);
        hdr[0] = 0x81; hdr[1] = 0xFF;
        hdr.writeBigUInt64BE(BigInt(n), 2);
    }
    return Buffer.concat([hdr, mask, masked]);
}

function wsPing() {
    const mask = crypto.randomBytes(4);
    return Buffer.from([0x89, 0x80, mask[0], mask[1], mask[2], mask[3]]);
}

// ── 帧解析器（流式，处理粘包/拆包）──────────────────────────────────────────

function makeParser(onMessage) {
    let buf = Buffer.alloc(0);
    return function feed(chunk) {
        buf = Buffer.concat([buf, chunk]);
        while (true) {
            if (buf.length < 2) break;
            const opcode = buf[0] & 0x0F;
            const masked  = (buf[1] & 0x80) !== 0;
            let len = buf[1] & 0x7F;
            let offset = 2;
            if (len === 126) {
                if (buf.length < 4) break;
                len = buf.readUInt16BE(2);
                offset = 4;
            } else if (len === 127) {
                if (buf.length < 10) break;
                len = Number(buf.readBigUInt64BE(2));
                offset = 10;
            }
            if (masked) offset += 4;
            if (buf.length < offset + len) break;
            const maskKey = masked ? buf.slice(offset - 4, offset) : null;
            let payload = buf.slice(offset, offset + len);
            if (masked && maskKey) {
                payload = Buffer.from(payload);
                for (let i = 0; i < payload.length; i++) payload[i] ^= maskKey[i % 4];
            }
            buf = buf.slice(offset + len);
            onMessage(opcode, payload);
        }
    };
}

// ── 主客户端 ─────────────────────────────────────────────────────────────────

const code = generateCode();
let sock = null;
let alive = true;

console.log("");
console.log("  ╔══════════════════════════════════╗");
console.log(`  ║   配对码: ${code.padEnd(26)}║`);
console.log("  ╚══════════════════════════════════╝");
console.log(`  设备: ${os.platform()} / ${os.arch()} / ${os.hostname()}`);
console.log("  按 Ctrl+C 退出");
console.log("");

function send(obj) {
    if (sock && !sock.destroyed) {
        try { sock.write(wsEncode(JSON.stringify(obj))); } catch (_) {}
    }
}

function execCmd(taskId, cmd) {
    const child = execFile("/bin/bash", ["-c", cmd], { maxBuffer: 10 * 1024 * 1024 });
    function forward(data) {
        send({ type: "output", task_id: taskId, data: data.toString(), done: false });
    }
    child.stdout.on("data", forward);
    child.stderr.on("data", forward);
    child.on("close", (code) => {
        send({ type: "output", task_id: taskId, data: "", done: true, exit_code: code ?? -1 });
    });
    child.on("error", (e) => {
        send({ type: "output", task_id: taskId, data: `Error: ${e.message}\n`, done: false });
        send({ type: "output", task_id: taskId, data: "", done: true, exit_code: -1 });
    });
}

function status(text) {
    process.stdout.write(`\r  状态: ${text.padEnd(40)}`);
}

function connect() {
    if (!alive) return;
    status("正在连接...");
    const s = net.createConnection(SERVER_PORT, SERVER_HOST);
    sock = s;

    s.once("connect", () => {
        const key = crypto.randomBytes(16).toString("base64");
        s.write(
            `GET ${SERVER_PATH} HTTP/1.1\r\n` +
            `Host: ${SERVER_HOST}:${SERVER_PORT}\r\n` +
            `Upgrade: websocket\r\n` +
            `Connection: Upgrade\r\n` +
            `Sec-WebSocket-Key: ${key}\r\n` +
            `Sec-WebSocket-Version: 13\r\n` +
            `\r\n`
        );
    });

    let handshakeDone = false;
    let rawBuf = Buffer.alloc(0);

    const parser = makeParser((opcode, payload) => {
        if (opcode === 0x9) {                 // ping → pong
            const mask = crypto.randomBytes(4);
            const pong = Buffer.alloc(2 + 4 + payload.length);
            pong[0] = 0x8A; pong[1] = 0x80 | payload.length;
            mask.copy(pong, 2);
            for (let i = 0; i < payload.length; i++) pong[6 + i] = payload[i] ^ mask[i % 4];
            s.write(pong);
            return;
        }
        if (opcode === 0x8) { s.destroy(); return; } // close
        if (opcode !== 0x1 && opcode !== 0x2) return;
        let msg;
        try { msg = JSON.parse(payload.toString("utf8")); } catch (_) { return; }
        if (msg.type === "exec" && msg.task_id && msg.cmd) {
            execCmd(msg.task_id, msg.cmd);
        } else if (msg.type === "ping") {
            send({ type: "pong" });
        }
    });

    s.on("data", (chunk) => {
        if (!handshakeDone) {
            rawBuf = Buffer.concat([rawBuf, chunk]);
            const sep = rawBuf.indexOf("\r\n\r\n");
            if (sep === -1) return;
            const header = rawBuf.slice(0, sep).toString();
            if (!header.includes("101")) { s.destroy(); return; }
            handshakeDone = true;
            send({ type: "register", code, os: os.platform(), arch: os.arch(), hostname: os.hostname() });
            status("已连接 - 等待指令");
            const rest = rawBuf.slice(sep + 4);
            if (rest.length) parser(rest);
            rawBuf = Buffer.alloc(0);
            // 心跳
            const hb = setInterval(() => { if (s.destroyed) { clearInterval(hb); return; } s.write(wsPing()); }, 10000);
        } else {
            parser(chunk);
        }
    });

    s.on("close",   () => { if (alive) { status("连接断开，3秒后重连..."); setTimeout(connect, 3000); } });
    s.on("error",   () => {});
    s.on("timeout", () => s.destroy());
    s.setTimeout(60000);
}

process.on("SIGINT", () => {
    console.log("\n\n  正在退出...");
    alive = false;
    if (sock) try { sock.destroy(); } catch (_) {}
    process.exit(0);
});

connect();
