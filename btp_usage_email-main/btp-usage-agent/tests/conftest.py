"""Put app/ on sys.path so tests can import uas_tool and friends directly."""
import sys
from pathlib import Path

_APP_DIR = str(Path(__file__).parent.parent / "app")
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
