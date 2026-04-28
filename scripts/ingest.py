import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ingest.loader import load_derby_field

if __name__ == "__main__":
    load_derby_field()
