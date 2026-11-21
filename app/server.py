"""Small reference service for Kubernetes rollout and monitoring demos."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = 0
    lock = threading.Lock()

    def do_GET(self):
        if self.path == "/metrics":
            with self.lock:
                count = self.requests
            self.respond(200, f"# TYPE release_demo_requests_total counter\nrelease_demo_requests_total {count}\n", "text/plain; version=0.0.4")
        elif self.path in ("/healthz", "/readyz"):
            self.respond(200, '{"status":"ok"}')
        elif self.path == "/":
            with self.lock:
                Handler.requests += 1
            self.respond(200, json.dumps({"service": "release-demo", "version": os.getenv("APP_VERSION", "dev")}))
        else:
            self.respond(404, '{"error":"not found"}')

    def respond(self, status, body, media_type="application/json"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", 8080), Handler) as server:
        server.serve_forever()
