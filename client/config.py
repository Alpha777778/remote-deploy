import os

SERVER_URL = os.getenv("SERVER_URL", "ws://localhost:5100/deploy/ws/client")

# Validation
if not SERVER_URL:
    raise ValueError("SERVER_URL environment variable is required")
