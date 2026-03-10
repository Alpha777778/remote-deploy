import os

HOST = "0.0.0.0"
PORT = 5100

# Codex (OpenAI Responses API)
CODEX_API_URL = "https://gmn.chuangzuoli.com/v1/responses"
CODEX_API_KEY = os.getenv("CODEX_API_KEY")
CODEX_MODEL = "gpt-5.4"

# Claude (OpenAI-compatible relay)
CLAUDE_API_URL = "https://hk.ioasis.xyz/v1/chat/completions"
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-6"

# TOTP (Google Authenticator) secrets per room
ROOM_TOTP_SECRETS = {
    "main":  os.getenv("DEPLOY_TOTP_SECRET",       "QRKPEOH5BHAOPKUP2YXA4FT7O4JOX2Q5"),
    "ren":   os.getenv("DEPLOY_TOTP_SECRET_REN",   "PNK3HI27NDICP2UZ6YG6QZD452VZ4OGD"),
    "cheng": os.getenv("DEPLOY_TOTP_SECRET_CHENG", "SJSZAHM3JPXBDHO7EEBQNXE3PQJ75TP"),
}

# Validation
if not CODEX_API_KEY:
    raise ValueError("CODEX_API_KEY environment variable is required")
if not CLAUDE_API_KEY:
    raise ValueError("CLAUDE_API_KEY environment variable is required")
