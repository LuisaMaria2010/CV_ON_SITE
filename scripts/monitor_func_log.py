"""Tails func_verbose.log and reports lines with ERROR/Exception/Traceback.
Writes matches to scripts/func_errors.log and echoes them to stdout.
Run in background (powershell terminal) with: .venv\Scripts\python scripts\monitor_func_log.py
"""
import time
import re
from pathlib import Path
from datetime import datetime

LOG = Path("func_verbose.log")
OUT = Path("scripts/func_errors.log")
PATTERN = re.compile(r"ERROR|Exception|Traceback", re.IGNORECASE)

print(f"Watcher starting, watching {LOG.resolve()}")
if not LOG.exists():
    print("Log file not found yet; waiting for creation...")
    while not LOG.exists():
        time.sleep(1)

with LOG.open("r", encoding="utf-8", errors="ignore") as fh:
    # seek to end
    fh.seek(0, 2)
    try:
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.5)
                continue
            if PATTERN.search(line):
                entry = f"[{datetime.utcnow().isoformat()}] {line.rstrip()}"
                print(entry)
                with OUT.open("a", encoding="utf-8") as out:
                    out.write(entry + "\n")
    except KeyboardInterrupt:
        print("Watcher stopped by user")
