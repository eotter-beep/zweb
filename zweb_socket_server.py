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
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List

from dns import build_error_page, describe

HOST = "127.0.0.1"
PORT = 65432
DEFAULT_DOMAIN_SERVER = "1.1.1.1"


@dataclass
class _ServerEntry:
    name: str
    address: str
    description: str = ""


class _RequestProcessor:
    """Utility wrapper that prepares responses for incoming commands."""

    def __init__(self, domain_server: str, extra_servers: Iterable[Dict[str, str]]) -> None:
        self._domain_server = domain_server
        self._servers: List[_ServerEntry] = [
            _ServerEntry("Domain server", domain_server, "Primary DNS authority"),
            _ServerEntry("Local helper", f"{HOST}:{PORT}", "Bundled lookup helper"),
        ]
        for entry in extra_servers:
            name = entry.get("name") or "Peer"
            address = entry.get("address") or ""
            description = entry.get("description", "")
            if not address:
                continue
            self._servers.append(_ServerEntry(name=name, address=address, description=description))

    def handle(self, raw: str) -> Dict[str, str]:
        request = raw.strip()
        if not request:
            return self._error_payload("no domain provided", request)

        command = request.upper()
        if command == "PING":
            return {"status": "ok", "message": "pong", "domain_server": self._domain_server}
        if command == "SERVERS":
            return {
                "status": "ok",
                "domain_server": self._domain_server,
                "servers": [asdict(entry) for entry in self._servers],
            }

        try:
            parts = describe(request)
        except ValueError as exc:
            return self._error_payload(str(exc), request)

        return {
            "status": "ok",
            "hostname": parts.hostname,
            "zone": parts.zone,
            "node": parts.node,
            "name": parts.name,
            "domain_server": self._domain_server,
        }

    def _error_payload(self, message: str, request: str) -> Dict[str, str]:
        return {
            "status": "error",
            "message": message,
            "domain_server": self._domain_server,
            "error_page": build_error_page(message, request),
        }


def serve(
    host: str = HOST,
    port: int = PORT,
    *,
    domain_server: str = DEFAULT_DOMAIN_SERVER,
    extra_servers: Iterable[Dict[str, str]] | None = None,
) -> None:
    """Run the TCP server until interrupted."""

    processor = _RequestProcessor(domain_server, extra_servers or [])

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
                    response = processor.handle(request)

                payload = json.dumps(response) + "\n"
                conn.sendall(payload.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    """Entrypoint used when executing the module as a script."""

    import argparse

    parser = argparse.ArgumentParser(description="Run the ZWeb DNS helper server")
    parser.add_argument("--host", default=HOST, help="Host/IP to bind the helper to")
    parser.add_argument("--port", type=int, default=PORT, help="Port to bind the helper to")
    parser.add_argument(
        "--domain-server",
        default=DEFAULT_DOMAIN_SERVER,
        help="Public IP of the authoritative domain server (default: %(default)s)",
    )
    parser.add_argument(
        "--extra-server",
        action="append",
        default=[],
        metavar="NAME=ADDRESS",
        help="Record an additional server in the directory (repeatable)",
    )

    args = parser.parse_args(argv)

    extra_servers = []
    for entry in args.extra_server:
        if "=" not in entry:
            name = "Peer"
            address = entry
        else:
            name, address = entry.split("=", 1)
        extra_servers.append({"name": name.strip(), "address": address.strip()})

    try:
        serve(
            host=args.host,
            port=args.port,
            domain_server=args.domain_server,
            extra_servers=extra_servers,
        )
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover - defensive logging only
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
