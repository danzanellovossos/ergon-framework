import os

from dotenv import load_dotenv


def load_env():
    # Load .env only when running locally
    if os.environ.get("ENV", "local") == "local":
        load_dotenv()
