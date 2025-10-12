"""Socket-based bridge exposing DNS helpers to local clients.

This module exposes a tiny TCP server that accepts newline-delimited domain
names and returns JSON payloads describing their DNS components.  It exists to
serve GUI front-ends (such as the PyQt-based "ZWeb" browser) which communicate
with the Python side using the standard :mod:`socket` module rather than
higher-level HTTP helpers.

Each incoming connection is treated as a single request.  The client is
expected to send a domain (or URL) terminated by a newline.  The server will
respond with a single JSON document terminated by a newline and close the
connection.  The payload contains:

``status``
    ``"ok"`` for successful lookups, or ``"error"`` when the input cannot be
    parsed.

``hostname``
    The normalised hostname derived from the provided input.  Only present for
    successful lookups.

``zone`` / ``node`` / ``name``
    DNS information derived from :mod:`dns`.  Only present for successful
    lookups.

``message``
    An error message that accompanies failed lookups.

  The server prints a ``READY`` message to standard output once it starts
  listening which allows any front-end to wait for readiness before issuing
  requests.
"""

from __future__ import annotations

import json
import socket
import sys
from typing import Dict

from dns import describe

HOST = "127.0.0.1"
PORT = 65432


def _handle_request(raw: str) -> Dict[str, str]:
    """Process a single request and return a response payload."""

    request = raw.strip()
    if not request:
        return {"status": "error", "message": "no domain provided"}

    if request.upper() == "PING":
        return {"status": "ok", "message": "pong"}

    try:
        parts = describe(request)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok",
        "hostname": parts.hostname,
        "zone": parts.zone,
        "node": parts.node,
        "name": parts.name,
    }


def serve(host: str = HOST, port: int = PORT) -> None:
    """Run the TCP server until interrupted."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen()
        print(f"READY {host}:{port}", flush=True)

        while True:
            conn, _addr = sock.accept()
            with conn:
                data = bytearray()
                while True:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if data.endswith(b"\n"):
                        break

                try:
                    request = data.decode("utf-8")
                except UnicodeDecodeError:
                    response = {
                        "status": "error",
                        "message": "request was not valid UTF-8",
                    }
                else:
                    response = _handle_request(request)

                payload = json.dumps(response) + "\n"
                conn.sendall(payload.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    """Entrypoint used when executing the module as a script."""

    try:
        serve()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover - defensive logging only
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
