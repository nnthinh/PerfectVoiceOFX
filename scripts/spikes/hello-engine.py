#!/usr/bin/env python3
"""Spike hello-engine — §3.8 serve contract, no Demucs/PyTorch.

spawn(absPath, ["serve", "--bind", "127.0.0.1", "--port", "0",
                "--token-file", tokenPath], {cwd: engineDir})
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _die(msg: str, code: int = 1) -> None:
    print(f"hello-engine: {msg}", file=sys.stderr)
    raise SystemExit(code)


def read_token_and_unlink(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        _die(f"cannot read token file: {exc}")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _die(f"unlink token: {exc}")
    if not token:
        _die("token file empty")
    return token


class HealthHandler(BaseHTTPRequestHandler):
    server_version = "perfectvoice-hello/0"
    token = ""

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        if not self._authorized():
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        if self.path.split("?", 1)[0] != "/v1/health":
            self._json(404, {"ok": False, "error": "not_found"})
            return
        self._json(200, {"ok": True, "protocol_version": 1})


def serve(bind: str, port: int, token_file: str) -> None:
    if bind != "127.0.0.1":
        _die("bind must be 127.0.0.1")
    token = read_token_and_unlink(token_file)
    HealthHandler.token = token
    httpd = ThreadingHTTPServer((bind, port), HealthHandler)
    host, ready_port = httpd.server_address
    print(f"READY http://{host}:{ready_port}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hello-engine")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--bind", required=True)
    serve_p.add_argument("--port", required=True, type=int)
    serve_p.add_argument("--token-file", required=True, dest="token_file")
    serve_p.add_argument("--token-fd", dest="token_fd", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if getattr(args, "token_fd", None) is not None:
        _die("--token-fd is not supported (use --token-file)")
    if args.cmd != "serve":
        parser.error("expected serve")
    serve(args.bind, args.port, args.token_file)


if __name__ == "__main__":
    main()
