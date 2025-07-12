# healthcheck.py — exits 0 if heartbeat updated within last 2 minutes

from pathlib import Path
from datetime import datetime, timedelta
import sys

HB = Path("/tmp/ttforwarder_heartbeat")
try:
    age = datetime.utcnow() - datetime.utcfromtimestamp(HB.stat().st_mtime)
except FileNotFoundError:          # no heartbeat yet (container just started)
    sys.exit(1)

sys.exit(0 if age < timedelta(minutes=15) else 1)
