"""Minimal API server for dashboard editable data.

Endpoints:
  GET  /api/feature-requests        — return feature_requests.json
  POST /api/feature-requests        — save feature_requests.json

Runs on 127.0.0.1:5000, proxied by nginx under /api/.
Auth is handled upstream by nginx (Okta/oauth2-proxy).
"""

import http.server
import json
import os
import sys

DATA_FILE = os.environ.get(
    "FEATURE_REQUESTS_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "feature_requests.json"),
)


class APIHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/api/feature-requests":
            try:
                with open(DATA_FILE) as f:
                    data = json.load(f)
                self._json(data)
            except FileNotFoundError:
                self._json([])
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/feature-requests":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                if not isinstance(data, list):
                    raise ValueError("expected a JSON array")
                for item in data:
                    if not isinstance(item.get("name"), str) or not isinstance(item.get("url"), str):
                        raise ValueError("each item needs 'name' and 'url' strings")
            except (json.JSONDecodeError, ValueError) as e:
                self._json({"error": str(e)}, 400)
                return
            os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
            with open(DATA_FILE, "w") as f:
                json.dump(data, f, indent=2)
            self._json({"ok": True})
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[api] {args[0]}\n")


if __name__ == "__main__":
    host, port = "127.0.0.1", 5000
    server = http.server.HTTPServer((host, port), APIHandler)
    print(f"Dashboard API on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
