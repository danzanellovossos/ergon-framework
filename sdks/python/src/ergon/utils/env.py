import os

from dotenv import load_dotenv


def load_env():
    env = os.environ.get("ENV", "local")
    load_dotenv(f".env.{env}", encoding="utf-8")
