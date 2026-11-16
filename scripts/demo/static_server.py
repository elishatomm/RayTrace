#!/usr/bin/env python3
"""A plain static file server that serves the recorded demo the way a static host does (test_demo.py starts it).

It serves one folder on 127.0.0.1, GET and HEAD only, and adds the response headers that folder's _headers file names,
in the syntax Cloudflare Pages and Netlify read: a line with a path pattern (`*` matches anything, `:name` one path
segment), then indented `Name: value` lines; `#` starts a comment. Like those hosts it does not serve _headers itself.
Each request is logged as one line (method, path, status) to --log. Standard library only.
"""
from __future__ import annotations

import argparse
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlsplit

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

HeaderRule = tuple[re.Pattern[str], list[tuple[str, str]]]


def path_pattern(path: str) -> re.Pattern[str]:
    parts = re.split(r"(\*|:[A-Za-z]\w*)", path)
    regex = "".join(".*" if part == "*" else "[^/]+" if part.startswith(":") else re.escape(part) for part in parts)
    return re.compile(f"^{regex}$")


def parse_headers_file(text: str) -> list[HeaderRule]:
    rules: list[HeaderRule] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            rules.append((path_pattern(stripped), []))
            continue
        name, separator, value = stripped.partition(":")
        if not rules or not separator or not name.strip():
            raise ValueError(f"_headers line {number} is not an indented 'Name: value' under a path: {line!r}")
        rules[-1][1].append((name.strip(), value.strip()))
    return rules


class StaticHandler(BaseHTTPRequestHandler):
    root: ClassVar[Path]
    rules: ClassVar[list[HeaderRule]]
    log_file: ClassVar[Path]
    server_version = "static"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches to
        self._serve(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - the name http.server dispatches to
        self._serve(include_body=False)

    def _locate(self, path: str) -> Path | None:
        if path == "/_headers":
            return None
        candidate = (self.root / path.lstrip("/")).resolve()
        if not candidate.is_relative_to(self.root):
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        return candidate if candidate.is_file() else None

    def _serve(self, *, include_body: bool) -> None:
        path = unquote(urlsplit(self.path).path)
        found = self._locate(path)
        status = HTTPStatus.OK if found is not None else HTTPStatus.NOT_FOUND
        body = found.read_bytes() if found is not None else b"not found\n"
        content_type = "text/plain; charset=utf-8" if found is None else CONTENT_TYPES.get(found.suffix,
                                                                                            "application/octet-stream")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for pattern, headers in self.rules:
            if pattern.match(path):
                for name, value in headers:
                    self.send_header(name, value)
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        with self.log_file.open("a", encoding="utf-8") as log:
            log.write(f"{self.command} {self.path} {code}\n")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the signature http.server calls
        with self.log_file.open("a", encoding="utf-8") as log:
            log.write(f"{format % args}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a folder on 127.0.0.1 with the headers of its _headers file.")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    headers_file = root / "_headers"
    rules = parse_headers_file(headers_file.read_text(encoding="utf-8")) if headers_file.is_file() else []
    handler = type("Handler", (StaticHandler,), {"root": root, "rules": rules, "log_file": args.log})
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"serving {root} on http://127.0.0.1:{args.port}/ with {len(rules)} header rule(s)", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
