#!/usr/bin/env python3
"""A continuous job that runs until killed."""

import signal
import sys
import time
from datetime import datetime

running = True

def handle_signal(signum, frame):
    global running
    print(f"\nReceived signal {signum}, shutting down gracefully...")
    running = False

def main():
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    print("Starting continuous job...")
    counter = 0
    
    while running:
        counter += 1
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] Heartbeat #{counter}")
        time.sleep(5)
    
    print("Continuous job stopped cleanly.")
    sys.exit(0)

if __name__ == "__main__":
    main()
