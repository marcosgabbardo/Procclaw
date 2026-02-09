#!/usr/bin/env python3
"""A job that writes heartbeat to a file periodically."""

import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

running = True
heartbeat_file = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/procclaw-test-heartbeat"))

def handle_signal(signum, frame):
    global running
    print(f"\nReceived signal {signum}, shutting down...")
    running = False

def write_heartbeat():
    timestamp = datetime.now().isoformat()
    heartbeat_file.write_text(timestamp)
    print(f"Wrote heartbeat: {timestamp}")

def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    print(f"Starting heartbeat job, writing to: {heartbeat_file}")
    
    while running:
        write_heartbeat()
        time.sleep(10)  # Write every 10 seconds
    
    print("Heartbeat job stopped.")
    sys.exit(0)

if __name__ == "__main__":
    main()
