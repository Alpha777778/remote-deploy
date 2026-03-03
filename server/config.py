import os

HOST = "0.0.0.0"
PORT = 5100

# Codex (OpenAI Responses API)
CODEX_API_URL = "https://gmn.chuangzuoli.com/v1/responses"
CODEX_API_KEY = os.getenv("CODEX_API_KEY")
CODEX_MODEL = "gpt-5.3-codex"

# Claude (OpenAI-compatible relay)
CLAUDE_API_URL = "https://hk.ioasis.xyz/v1/chat/completions"
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-6"

# TOTP (Google Authenticator) secret for admin login
TOTP_SECRET = os.getenv("DEPLOY_TOTP_SECRET")

# Validation
if not CODEX_API_KEY:
    raise ValueError("CODEX_API_KEY environment variable is required")
if not CLAUDE_API_KEY:
    raise ValueError("CLAUDE_API_KEY environment variable is required")
if not TOTP_SECRET:
    raise ValueError("DEPLOY_TOTP_SECRET environment variable is required")
