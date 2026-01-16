import os
from dotenv import load_dotenv


def load_env():
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