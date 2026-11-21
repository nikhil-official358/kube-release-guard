"""Loopback dashboard for Docker Capacity Pilot."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parent
ASSETS = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true")
    source.add_argument("--context")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--ollama")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh", type=int, default=15)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.refresh < 10:
        parser.error("Choose a port from 1024 to 65535 and a refresh interval of at least 10 seconds.")
    command = [sys.executable, str(PROJECT / "agent.py")]
    if args.demo:
        command.append("--demo")
    else:
        command.extend([f"--context={args.context}", f"--namespace={args.namespace}"])
    if args.ollama:
        command.append(f"--ollama={args.ollama}")
    scope = "Sample workload" if args.demo else f"{args.context} / {args.namespace}"

    class Dashboard(BaseHTTPRequestHandler):
        def reply(self, status, content_type, content):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            if (self.headers.get("Host") not in {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
                    or self.headers.get("Sec-Fetch-Site") == "cross-site"):
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path in ASSETS:
                filename, content_type = ASSETS[path]
                self.reply(200, content_type, (PROJECT / filename).read_bytes())
                return
            if path != "/api/snapshot":
                self.send_error(404)
                return
            payload = {"scope": scope, "refresh": args.refresh}
            try:
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                        errors="replace", timeout=95, cwd=PROJECT)
                if result.returncode:
                    raise ValueError(result.stderr.strip() or "The agent did not return a report.")
                payload["report"] = json.loads(result.stdout)
                status = 200
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                payload["error"] = str(error)
                status = 503
            self.reply(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    with HTTPServer(("127.0.0.1", args.port), Dashboard) as server:
        print(f"Open http://127.0.0.1:{args.port}/ (Ctrl+C to stop)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
