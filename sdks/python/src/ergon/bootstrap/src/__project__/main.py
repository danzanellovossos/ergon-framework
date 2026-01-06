from ergon.cli import ergon
from ergon.utils.env import load_env

# Load env once
load_env()

def main():
    ergon()
