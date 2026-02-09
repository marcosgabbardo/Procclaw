#!/usr/bin/env python3
"""A job that always fails after a short time."""

import sys
import time

def main():
    print("Starting failing job...")
    time.sleep(1)
    print("ERROR: Simulated failure!")
    sys.exit(1)

if __name__ == "__main__":
    main()
