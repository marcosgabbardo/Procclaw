#!/usr/bin/env python3
"""A simple job that runs for a few seconds and exits successfully."""

import sys
import time

def main():
    print("Starting success job...")
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    
    for i in range(duration):
        print(f"Working... {i+1}/{duration}")
        time.sleep(1)
    
    print("Success job completed!")
    sys.exit(0)

if __name__ == "__main__":
    main()
