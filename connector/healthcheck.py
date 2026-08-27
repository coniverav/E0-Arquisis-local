from pathlib import Path
import sys
import time

HEARTBEAT = Path("/tmp/energyshark_connector_heartbeat")
MAX_AGE_SECONDS = 60

try:
    age = time.time() - HEARTBEAT.stat().st_mtime
except FileNotFoundError:
    sys.exit(1)

sys.exit(0 if age <= MAX_AGE_SECONDS else 1)
