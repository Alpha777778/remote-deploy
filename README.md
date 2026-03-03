# 远程部署工具

通过浏览器用自然语言控制远程机器执行命令，AI 自动将指令转换为 Shell 命令并实时返回结果。

## 架构

```
管理员浏览器  ──WebSocket──▶  服务端 (your-server:5100)  ──WebSocket──▶  客户端 (员工机器)
     │                              │
  输入指令                      Codex AI
  查看结果                    自动生成命令
```

## 功能

- **自然语言控制** — 输入"帮他安装 node"，AI 自动执行安装命令
- **实时输出** — 命令执行过程逐行推送到浏览器
- **多设备管理** — 左侧设备列表，支持多台机器同时在线
- **会话持久** — 对话历史保存在本地，服务器重启不丢失
- **自动重连** — 客户端和管理员断线后自动重连
- **24h 登录** — 管理员验证码登录后 24 小时内免重新输入

## 目录结构

```
├── server/          # FastAPI + WebSocket 服务端
│   ├── main.py      # 主程序（路由、WS 处理、session 管理）
│   ├── codex_agent.py  # AI 代理（调用 Codex API，执行多轮命令）
│   ├── config.py    # 服务器配置（端口、API Key、TOTP 密钥）
│   └── static/
│       └── index.html  # 管理员控制台（单文件前端）
├── client/          # Windows 客户端
│   ├── client.py    # tkinter GUI + WebSocket
│   ├── config.py    # 服务器地址配置
│   └── dist/
│       └── 远程部署客户端.exe  # 打包好的可执行文件
└── client_mac/      # macOS 客户端
    ├── client.py    # tkinter GUI + WebSocket
    └── 远程部署客户端.command  # 双击运行（自动安装依赖）
```

## 配置

在开始之前，需要配置环境变量：

### 1. 服务端配置

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入您的配置：
# CODEX_API_KEY=your_codex_api_key_here
# CLAUDE_API_KEY=your_claude_api_key_here
# DEPLOY_TOTP_SECRET=your_totp_secret_here
```

### 2. 客户端配置

客户端同样需要配置服务器地址，在客户端目录创建 `.env` 文件：

```bash
# client/.env 或 client_mac/.env
SERVER_URL=ws://your_server_ip:5100/deploy/ws/client
```

## 部署服务端

```bash
cd server
pip install -r requirements.txt
python main.py
```

默认监听 `0.0.0.0:5100`，管理员面板：`http://服务器IP:5100/deploy/`

## 使用客户端

**Windows** — 双击 `远程部署客户端.exe`，窗口显示 4 位配对码

**macOS** — 双击 `远程部署客户端.command`，首次运行自动安装 Python 和依赖

客户端启动后会在管理员面板的设备列表中出现，选中设备即可发送指令。

## 管理员登录

访问控制台需要 Google Authenticator 6 位 TOTP 验证码，登录后 Session 有效期 24 小时，服务器重启不影响登录状态。

## 重新打包客户端

```bash
# Windows
cd client
pyinstaller RemoteDeploy.spec --noconfirm

# macOS（需要在 Mac 上执行）
cd client_mac
bash build_mac.sh
```
