from ergon_framework.cli import ergon
from ergon_framework.utils.env import load_env

# Load env once
load_env()


if __name__ == "__main__":
    ergon()
