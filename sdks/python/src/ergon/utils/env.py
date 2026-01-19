import os
from dotenv import load_dotenv

ENV_LOADED = False


def load_env():
    global ENV_LOADED
    if ENV_LOADED:
        return

    env_file = os.environ.get("ENV_FILE")

    if not env_file:
        raise ValueError(
            "ENV_FILE is not set. You must set the ENV_FILE environment variable. "
            "Example: ENV_FILE=.env.local | .env.dev | .env.prod | .env.staging"
        )

    # If env vars already exist, we are likely in Docker/K8s
    # dotenv should NOT override them
    if os.path.exists(env_file):
        load_dotenv(env_file, encoding="utf-8", override=True)

    ENV_LOADED = True