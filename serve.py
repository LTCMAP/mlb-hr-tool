#!/usr/bin/env python3
"""Tiny static file server for local preview of the HR tool.
Usage: python3 serve.py [port]   (default 8765)
"""
import sys, os, http.server, socketserver

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
DIRECTORY = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIRECTORY, **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Serving {DIRECTORY} at http://localhost:{PORT}")
    httpd.serve_forever()
