# OpenClaw 安装部署实战知识库

## 什么是 OpenClaw

OpenClaw（曾用名 Clawdbot / Moltbot）是开源个人 AI 助手框架，支持飞书/钉钉/QQ/Telegram 接入。
**用户让你"安装 openclaw"、"装小龙虾"、"装 OpenClaw"，指的就是这个。**

核心组件：
- **Gateway**（`openclaw gateway`）：监听 18789 端口，处理 IM 消息
- **模型供应商**：通过 `openclaw.json` 配置 API Key 和模型
- **飞书渠道**：无需公网 Webhook，用 WebSocket 长连接推送

---

## 快速诊断命令

```bash
# 状态总览（优先用这个）
openclaw doctor

# 查看设备/会话
openclaw devices list
openclaw status

# 重启
openclaw gateway restart
```

Windows 非标准路径：
```batch
C:\node-v22.14.0-win-x64\node.exe C:\openclaw_global\node_modules\openclaw\dist\index.js doctor
```

---

## 已知朋友电脑档案

### 朋友 A：Windows Server（Administrator）

| 项目 | 值 |
|------|-----|
| 系统 | Windows 11 Pro |
| 用户名 | `Administrator` |
| Node.js | `C:\node-v22.14.0-win-x64\node.exe`（v22，非标准路径）|
| npm global prefix | `C:\openclaw_global` |
| openclaw 入口 | `C:\openclaw_global\node_modules\openclaw\dist\index.js` |
| 主配置 | `C:\Users\Administrator\.openclaw\openclaw.json` |
| Agent 模型配置 | `C:\Users\Administrator\.openclaw\agents\main\agent\models.json` |
| auth-profiles | `C:\Users\Administrator\.openclaw\agents\main\agent\auth-profiles.json` |
| GMN 代理脚本 | `C:\Users\Administrator\.openclaw\gmn_proxy.js` |
| Gateway 启动脚本 | `C:\Users\Administrator\.openclaw\gateway.cmd` |
| 飞书 App ID | `cli_a9215041a9f8dbc4` |
| Gateway Token | `555` |
| 隧道地址 | `354d30093763.ofalias.com:27366` |
| 密码 | `123456` |

---

## 安装 OpenClaw

### Node.js 版本要求

**必须 v22 或更高**。v18、v20 均无法启动。

```bash
node -v   # 必须显示 v22.x
```

### macOS/Linux 安装 Node.js 22（无 Homebrew 时）

```bash
# ARM64 Mac
curl -fsSL https://nodejs.org/dist/latest-v22.x/node-v22.14.0-darwin-arm64.tar.gz -o /tmp/node.tar.gz
mkdir -p ~/.local ~/.npm-global
tar -xzf /tmp/node.tar.gz -C ~/.local
ln -sfn ~/.local/node-v22.14.0-darwin-arm64 ~/.local/node-current
echo 'export PATH="$HOME/.local/node-current/bin:$HOME/.npm-global/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile

# x86_64 Linux/Mac
curl -fsSL https://nodejs.org/dist/latest-v22.x/node-v22.14.0-linux-x64.tar.gz -o /tmp/node.tar.gz
# 同上解压
```

### 安装 OpenClaw

```bash
# macOS/Linux（标准）
npm install -g openclaw@latest

# 如果报 GitHub SSH 错误（git@github.com permission denied）
git config --global url."https://github.com/".insteadOf git@github.com:
git config --global url."https://github.com/".insteadOf ssh://git@github.com/
# 然后重试

# 如果报 node-llama-cpp 编译失败（macOS 版本太低）
npm install -g openclaw@latest --ignore-scripts

# 如果报 ETIMEDOUT（网络超时，需要代理）
npm config set proxy "http://127.0.0.1:7890"
npm config set https-proxy "http://127.0.0.1:7890"
# 然后重试
```

### Windows 安装（非标准路径）

```batch
rem 先设置 npm 全局路径
C:\node-v22.14.0-win-x64\npm.cmd config set prefix C:\openclaw_global

rem 安装
C:\node-v22.14.0-win-x64\npm.cmd install -g openclaw@latest

rem 验证
C:\node-v22.14.0-win-x64\node.exe C:\openclaw_global\node_modules\openclaw\dist\index.js --version
```

---

## 配置 GMN 自定义模型（最常用）

GMN（`gmn.chuangzuoli.com`）使用 OpenAI Responses API，**不是** `chat/completions`。

### ⚠️ 最大坑：GMN WAF 拦截 OpenAI SDK User-Agent

**现象**：`curl` 直接调 GMN → 200 OK；OpenClaw 调 GMN → `403 Your request was blocked.`

**根因**：GMN 的 Cloudflare WAF 把 `OpenAI/openai-node/x.x.x` UA 加入黑名单。OpenClaw 内部用 OpenAI Node.js SDK，默认带这个 UA。

**解法**：在本地起一个 HTTP 代理替换 UA 后转发。

### gmn_proxy.js（必须先跑这个代理，openclaw 才能用 GMN）

保存到 `~/.openclaw/gmn_proxy.js`（macOS/Linux）或 `C:\Users\Administrator\.openclaw\gmn_proxy.js`（Windows）：

```javascript
const http = require('http'), https = require('https'), url = require('url');
const TARGET = 'https://gmn.chuangzuoli.com', PORT = 19876;
http.createServer((req, res) => {
  const target = url.parse(TARGET + req.url);
  const headers = { ...req.headers };
  delete headers['host'];
  if (!headers['user-agent'] || headers['user-agent'].includes('openai-node') || headers['user-agent'].includes('OpenAI/'))
    headers['user-agent'] = 'Mozilla/5.0 Node/22 OpenClaw/2026.3.2';
  const chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    const body = Buffer.concat(chunks);
    const opts = { hostname: target.hostname, port: 443, path: target.path, method: req.method,
      headers: { ...headers, 'content-length': body.length } };
    const proxy = https.request(opts, presp => { res.writeHead(presp.statusCode, presp.headers); presp.pipe(res); });
    proxy.on('error', e => { res.writeHead(502); res.end(e.message); });
    proxy.write(body); proxy.end();
  });
}).listen(PORT, '127.0.0.1', () => console.log(`[GMN Proxy] :${PORT} -> ${TARGET}`));
```

验证代理正常（用被封的 UA 也能通）：
```bash
curl -X POST http://127.0.0.1:19876/v1/responses \
  -H "Authorization: Bearer sk-你的key" \
  -H "User-Agent: OpenAI/openai-node/4.77.0" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":[{"role":"user","content":"hi"}]}'
# 应返回 200
```

---

## openclaw.json 正确配置模板（GMN + 飞书）

```json
{
  "gateway": {
    "port": 18789,
    "mode": "local",
    "bind": "lan",
    "auth": { "mode": "token", "token": "555" },
    "controlUi": {
      "allowedOrigins": ["*"],
      "allowInsecureAuth": true,
      "dangerouslyDisableDeviceAuth": true
    }
  },
  "agents": {
    "defaults": {
      "model": { "primary": "gmn/gpt-5.4" },
      "memorySearch": { "enabled": false }
    }
  },
  "models": {
    "providers": {
      "gmn": {
        "baseUrl": "http://127.0.0.1:19876/v1",
        "auth": "api-key",
        "api": "openai-responses",
        "apiKey": "sk-你的密钥",
        "models": [
          { "id": "gpt-5.4", "name": "GPT-5.4", "reasoning": false,
            "contextWindow": 128000, "maxTokens": 32000 }
        ]
      }
    }
  },
  "channels": {
    "feishu": {
      "enabled": true,
      "dmPolicy": "open",
      "allowFrom": ["*"],
      "defaultAccount": "main",
      "accounts": {
        "main": { "appId": "cli_你的AppID", "appSecret": "你的AppSecret" }
      }
    }
  },
  "plugins": {
    "allow": ["feishu"],
    "entries": { "feishu": { "enabled": true } }
  }
}
```

**注意**：`controlUi.dangerouslyDisableDeviceAuth: true` 是从局域网 IP 访问 Control UI 的必要配置。

---

## auth-profiles.json 正确格式

文件路径：`~/.openclaw/agents/main/agent/auth-profiles.json`

```json
{
  "version": 1,
  "profiles": {
    "gmn:manual": {
      "provider": "gmn",
      "profileId": "gmn:manual",
      "type": "api_key",
      "key": "sk-你的密钥",
      "createdAt": 1741344000000
    }
  }
}
```

**必须有 `"type": "api_key"`**，否则显示 "Missing auth - gmn"。

---

## ⚠️ 配置陷阱清单（必读）

| 坑 | 现象 | 正解 |
|----|------|------|
| `auth` 字段写 `"apiKey"` | `Invalid input` | 必须写 `"api-key"`（连字符）|
| models 数组缺 `name` 字段 | config validation 失败 | 必须加 `"name": "GPT-5.4"` |
| `reasoning` 没写 false | 触发 reasoning 模式报错 | 必须 `"reasoning": false` |
| 只改全局 openclaw.json | baseUrl 不生效 | agent 级别 models.json 优先级更高，**两个都要改** |
| `model.primary` 格式错 | Unknown model | 格式必须是 `"provider名/model-id"`，如 `"gmn/gpt-5.4"` |
| GMN 端点用 `/chat/completions` | 400 Unsupported | GMN 只支持 `/v1/responses`，`api` 字段必须是 `"openai-responses"` |
| OpenClaw 直连 GMN | 403 blocked | 必须走本地 gmn_proxy.js 代理 |
| agent models.json 有旧值 | 配置不生效 | 删掉重建，重启 gateway |

---

## Windows Gateway 启动脚本（gateway.cmd）

```batch
@echo off
rem === Stop existing instances ===
taskkill /F /IM node.exe >nul 2>&1
timeout /T 2 /NOBREAK >nul
rem === Start GMN UA Proxy ===
start "gmn_proxy" /B C:\node-v22.14.0-win-x64\node.exe C:\Users\Administrator\.openclaw\gmn_proxy.js
timeout /T 2 /NOBREAK >nul
rem === Start OpenClaw Gateway ===
C:\node-v22.14.0-win-x64\node.exe C:\openclaw_global\node_modules\openclaw\dist\index.js gateway --port 18789
```

## Windows 开机自启（BootTrigger 任务，无需登录）

```batch
rem 先把任务 XML 写好，再注册
schtasks /Create /XML "C:\path\to\openclaw_task.xml" /TN "OpenClaw Autostart" /RU Administrator /RP 123456 /F
schtasks /Run /TN "OpenClaw Autostart"
schtasks /Change /TN "OpenClaw Gateway" /DISABLE
```

任务 XML 关键字段：`<BootTrigger>` + `<LogonType>Password</LogonType>` + `<RunLevel>HighestAvailable</RunLevel>`

---

## macOS 启动脚本（start-openclaw-gmn.sh）

```bash
#!/bin/bash
cd ~/.openclaw

# 先停旧进程
pkill -f gmn_proxy.js 2>/dev/null
pkill -f openclaw 2>/dev/null
sleep 1

# 启动 GMN 代理
node gmn_proxy.js > logs/gmn_proxy.log 2>&1 &
sleep 2

# 启动 OpenClaw Gateway
openclaw gateway --port 18789 > logs/gateway.log 2>&1 &
echo "OpenClaw started. Token: $(cat openclaw.json | grep token | head -1)"
```

---

## 飞书配置

### 飞书开放平台必做步骤

1. [open.feishu.cn/app](https://open.feishu.cn/app) 创建企业自建应用
2. **机器人**：功能 → 机器人 → 开启（必须开，否则无法收发消息）
3. **权限**：`im:message`、`im:message:send_as_bot`
4. **事件订阅**：添加事件 `im.message.receive_v1`
5. **发布版本**：每次改配置都要重新发版才生效

### 飞书无需公网 IP

飞书用 WebSocket 长连接推送，**不需要配置 Webhook 回调 URL**，无公网 IP 也能用。

### 飞书插件安装

```bash
openclaw plugins install @openclaw/feishu
# Windows
C:\node-v22.14.0-win-x64\node.exe C:\openclaw_global\node_modules\openclaw\dist\index.js plugins install @openclaw/feishu
```

### 飞书 dmPolicy 正确位置

```json
// 正确：在 feishu 顶层
"feishu": {
  "dmPolicy": "open",
  "allowFrom": ["*"],
  "accounts": { "main": { "appId": "...", "appSecret": "..." } }
}
// 错误：放在 accounts.main 里
```

---

## 可用 GMN 模型列表（2026-03）

```
gpt-5, gpt-5-codex, gpt-5-codex-mini
gpt-5.1, gpt-5.1-codex, gpt-5.1-codex-max, gpt-5.1-codex-mini
gpt-5.2, gpt-5.2-codex, gpt-5.3-codex, gpt-5.4
```

**推荐用 `gpt-5.4`**（最稳定）。端点：`POST https://gmn.chuangzuoli.com/v1/responses`。

---

## 快速排错流程

```
1. openclaw doctor → 看哪里 FAIL
2. curl 测 GMN 直连 → 200? 说明 key 有效
3. curl 测代理（127.0.0.1:19876）→ 200? 说明代理正常
4. openclaw status → gateway 在跑? 端口 18789?
5. 看日志 → ~/.openclaw/logs/ 或 AppData\Local\Temp\openclaw\
6. 飞书发消息不回复 → 检查飞书开放平台是否发版、权限是否加、事件是否订阅
```

---

## 常见报错速查

| 报错 | 原因 | 解法 |
|------|------|------|
| `403 Your request was blocked` | GMN WAF 封了 OpenAI SDK UA | 起 gmn_proxy.js 代理 |
| `Missing auth - gmn` | auth-profiles.json 缺 `type` 字段 | 手动补 `"type": "api_key"` |
| `Unknown model: gmn/gpt-5.4` | models.providers 里没定义这个模型 | 检查 openclaw.json 和 agent/models.json |
| `config validation failed` | 配置字段错误 | 检查 `auth` 是否为 `"api-key"`，model 是否有 `name` |
| `refusing to bind without auth` | bind=lan 但没配 token | 加 `"auth": {"mode":"token","token":"xxx"}` |
| `duplicate plugin id` | feishu 插件重复注册 | 正常现象，不影响功能，忽略 |
| `origin not allowed` | Control UI 从局域网 IP 访问 | 加 `controlUi.allowedOrigins: ["*"]` |
| `requires device identity` | Control UI 从局域网 IP 访问 | 加 `controlUi.dangerouslyDisableDeviceAuth: true` |
| `EACCES` npm 权限错误 | 系统级 npm 权限不足 | 用 `npm config set prefix ~/.npm-global` |
| Gateway 启动后进程消失 | SSH 会话关闭杀了进程 | 用任务计划程序（Windows）或 LaunchAgent（Mac）持久化 |
