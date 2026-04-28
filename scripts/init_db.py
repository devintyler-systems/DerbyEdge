import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.db import init_db

if __name__ == "__main__":
    init_db()
