import os
from dotenv import load_dotenv


global ENV_LOADED
ENV_LOADED = False


def load_env():
    
    global ENV_LOADED
    if ENV_LOADED:
        return

    env_file = os.environ.get("ENV_FILE")

    if not env_file:
        raise ValueError(
            "ENV_FILE is not set. You must set the ENV_FILE environment variable."
            "Example: ENV_FILE=.env.local"
            "Example: ENV_FILE=.env.dev"
            "Example: ENV_FILE=.env.prod"
            "Example: ENV_FILE=.env.staging"
        )

    load_dotenv(env_file, encoding="utf-8", override=False)
    ENV_LOADED = True