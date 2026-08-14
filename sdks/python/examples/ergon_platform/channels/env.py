import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

ERGON_CLIENT_ID = os.getenv("ERGON_CLIENT_ID", "")
ERGON_CLIENT_SECRET = os.getenv("ERGON_CLIENT_SECRET", "")
ERGON_BASE_URL = os.getenv("ERGON_BASE_URL") or "https://platform.ergondata.ai"

CHANNELS_CONFIG_ID = os.getenv("CHANNELS_CONFIG_ID", "")
CHANNELS_INSTRUCTIONS_ADDRESS = os.getenv("CHANNELS_INSTRUCTIONS_ADDRESS", "")
CHANNELS_AUTH_CODE_ADDRESS = os.getenv("CHANNELS_AUTH_CODE_ADDRESS", "")

CHANNELS_BATCH_SIZE = int(os.getenv("CHANNELS_BATCH_SIZE", "20"))
CHANNELS_STREAMING = os.getenv("CHANNELS_STREAMING", "false").lower() == "true"
