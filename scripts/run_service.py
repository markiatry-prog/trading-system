#!/usr/bin/env python3
"""Deployable entrypoint for the T-001 foundation.

Serves exactly one thing: GET /health. That is the whole point -- it
proves the isolated stack (repository -> container -> Railway -> its own
Supabase project) deploys and runs, without implementing any pipeline
stage to demonstrate it.

Uses the standard library's HTTP server deliberately: a foundation
service that exists to prove deployability should not drag in a web
framework it will likely replace once real work arrives.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading_system import db, env, health  # noqa: E402


def _reader():
    """The control capability is used for the health read: it is the one
    identity that exists purely to observe and steer, and giving the
    health surface a pipeline identity would mean deploying a pipeline
    credential for a stage that does not exist yet."""
    dsn = db.dsn_for("trading_control")
    try:
        import psycopg2
    except ImportError:
        class _Unavailable:
            def read_system_state(self):
                raise RuntimeError("psycopg2 is not installed")
        return _Unavailable()
    return db.StateReader(psycopg2.connect, dsn)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] != "/health":
            self.send_response(404)
            self.end_headers()
            return
        payload = health.report(_reader()).as_dict()
        body = json.dumps(payload).encode()
        # 200 when reachable, 503 when we cannot see our own database.
        # A paused-but-reachable system returns 200: paused is a healthy,
        # intended state, not a fault.
        self.send_response(200 if payload["reachable"] else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Never log request lines: they can carry query strings.
        sys.stdout.write(f"trading-system: {fmt % args}\n")
        sys.stdout.flush()


def main() -> int:
    port = int(env.get("PORT", "8080"))
    print(f"trading-system: serving /health on 0.0.0.0:{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
