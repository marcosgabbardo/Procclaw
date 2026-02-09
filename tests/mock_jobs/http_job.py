#!/usr/bin/env python3
"""A job that exposes an HTTP health endpoint."""

import os
import signal
import sys
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

running = True
port = int(os.environ.get("HTTP_PORT", "8765"))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            timestamp = datetime.now().isoformat()
            self.wfile.write(f'{{"status": "running", "timestamp": "{timestamp}"}}'.encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Custom logging
        print(f"[HTTP] {self.address_string()} - {format % args}")

def handle_signal(signum, frame):
    global running
    print(f"Received signal {signum}, shutting down...", flush=True)
    running = False

def main():
    global running
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    print(f"Starting HTTP job on port {port}...", flush=True)
    
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
    except OSError as e:
        print(f"ERROR: Could not bind to port {port}: {e}", flush=True)
        sys.exit(1)
    
    server.timeout = 1  # Check for shutdown every second
    
    print(f"Health endpoint: http://localhost:{port}/health", flush=True)
    print("Server ready.", flush=True)
    
    while running:
        server.handle_request()
    
    server.server_close()
    print("HTTP job stopped.", flush=True)
    sys.exit(0)

if __name__ == "__main__":
    main()
