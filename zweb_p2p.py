"""Utility helpers providing a lightweight P2P-inspired cache for ZWeb.

The original ZWeb experience only offered DNS lookups.  This module expands on
that foundation by adding a *very* small peer-to-peer style helper that can be
used by the GUI to "install" websites locally.  The implementation is not a
full BitTorrent client – that would be far beyond the scope of this project –
but it behaves similarly from the browser's point of view:

* The :class:`P2PManager` creates ``cache`` and ``data`` folders on demand.  The
  cache folder is considered volatile while the data folder is preserved across
  sessions for user specific state.
* When the GUI requests a site installation the manager downloads the primary
  HTML payload and stores it inside ``cache/<alias>/site.html``.  Metadata about
  the lookup is persisted in ``data/sites.json`` so that front-ends can rebuild
  state on the next launch if required.
* A tiny TCP server is provided so the GUI can expose the cached content on a
  well known IP address.  The default public address is ``1.1.1.1`` to satisfy
  the requirement that ZWeb peers share content through that endpoint, but the
  caller can request any bindable address.
* When the application exits the cache folder is scrubbed to avoid leaving
  behind temporary artefacts while the ``data`` directory remains intact.

The network component is intentionally conservative: each connection receives a
small JSON summary of the cached sites.  This is sufficient for the GUI to
demonstrate peer discovery without having to ship full website contents across
the wire inside this environment.
"""

from __future__ import annotations

import atexit
import json
import socket
import socketserver
import threading
import urllib.error
import urllib.request
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

DEFAULT_PUBLIC_IP = "1.1.1.1"
DEFAULT_PORT = 6881


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


@dataclass
class CachedSite:
    """Description of a cached site."""

    alias: str
    source: str
    hostname: str
    cache_path: Path
    data_path: Path

    def to_dict(self) -> Dict[str, str]:
        return {
            "alias": self.alias,
            "source": self.source,
            "hostname": self.hostname,
            "cache_path": str(self.cache_path),
            "data_path": str(self.data_path),
        }


class _PeerRequestHandler(socketserver.BaseRequestHandler):
    """Return a JSON summary of cached sites to connected peers."""

    def handle(self) -> None:  # pragma: no cover - relies on sockets
        manager: "P2PManager" = self.server.manager  # type: ignore[attr-defined]
        try:
            raw = self.request.recv(1024)
        except OSError:
            return

        message = (raw or b"").decode("utf-8", "ignore").strip().upper()
        if message == "PING":
            response = {"status": "ok", "message": "pong"}
        else:
            response = {
                "status": "ok",
                "public_ip": manager.public_ip,
                "sites": [site.to_dict() for site in manager.cached_sites],
            }

        payload = json.dumps(response).encode("utf-8") + b"\n"
        try:
            self.request.sendall(payload)
        except OSError:
            return


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, manager):  # pragma: no cover - thin wrapper
        self.manager = manager
        super().__init__(server_address, RequestHandlerClass)


@dataclass
class P2PManager:
    """Coordinate P2P style caching, persistence, and peer serving."""

    cache_dir: Path = field(default_factory=lambda: Path("cache"))
    data_dir: Path = field(default_factory=lambda: Path("data"))
    metadata_file: Path = field(init=False)
    _server: Optional[_ThreadedTCPServer] = field(default=None, init=False, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    public_ip: str = field(default=DEFAULT_PUBLIC_IP, init=False)
    cached_sites: list[CachedSite] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        _ensure_directory(self.cache_dir)
        _ensure_directory(self.data_dir)
        self.metadata_file = self.data_dir / "sites.json"
        self._load_metadata()
        atexit.register(self.cleanup_on_exit)

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------
    def _load_metadata(self) -> None:
        if not self.metadata_file.exists():
            self.cached_sites = []
            return

        try:
            data = json.loads(self.metadata_file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            self.cached_sites = []
            return

        sites: list[CachedSite] = []
        for entry in data:
            alias = entry.get("alias")
            source = entry.get("source")
            hostname = entry.get("hostname")
            cache_path = entry.get("cache_path")
            data_path = entry.get("data_path")
            if not all([alias, source, hostname, cache_path, data_path]):
                continue
            sites.append(
                CachedSite(
                    alias=str(alias),
                    source=str(source),
                    hostname=str(hostname),
                    cache_path=Path(cache_path),
                    data_path=Path(data_path),
                )
            )
        self.cached_sites = sites

    def _write_metadata(self) -> None:
        data = [site.to_dict() for site in self.cached_sites]
        try:
            self.metadata_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            return

    # ------------------------------------------------------------------
    # Cache operations
    # ------------------------------------------------------------------
    def _cache_path_for(self, alias: str) -> Path:
        safe_alias = alias.replace("/", "-") or "unknown"
        return self.cache_dir / safe_alias

    def _data_path_for(self, alias: str) -> Path:
        safe_alias = alias.replace("/", "-") or "unknown"
        return self.data_dir / safe_alias

    def install_site(self, source: str, alias: str, hostname: str) -> CachedSite:
        """Download ``source`` and record it as ``alias``.

        The function returns a :class:`CachedSite` that describes the cached
        payload.  Errors during the download are surfaced as :class:`RuntimeError`
        instances with user-friendly messages.
        """

        normalised_source = source.strip()
        if "://" not in normalised_source:
            normalised_source = f"http://{normalised_source}"

        cache_path = self._cache_path_for(alias)
        data_path = self._data_path_for(alias)
        _ensure_directory(cache_path)
        _ensure_directory(data_path)

        target_file = cache_path / "site.html"
        data_file = data_path / "site.html"
        try:
            with urllib.request.urlopen(normalised_source) as response:
                content = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"failed to download site: {exc}") from exc

        try:
            target_file.write_bytes(content)
            data_file.write_bytes(content)
        except OSError as exc:
            raise RuntimeError(f"unable to store site cache: {exc}") from exc

        site = CachedSite(
            alias=alias,
            source=normalised_source,
            hostname=hostname,
            cache_path=cache_path,
            data_path=data_path,
        )
        self._register_site(site)
        return site

    def _register_site(self, site: CachedSite) -> None:
        self.cached_sites = [existing for existing in self.cached_sites if existing.alias != site.alias]
        self.cached_sites.append(site)
        self._write_metadata()

    def mark_site_cached(self, alias: str) -> None:
        """Ensure the cache directory exists for ``alias``."""

        cache_path = self._cache_path_for(alias)
        data_path = self._data_path_for(alias)
        _ensure_directory(cache_path)
        _ensure_directory(data_path)
        if alias not in [site.alias for site in self.cached_sites]:
            site = CachedSite(alias=alias, source="", hostname="", cache_path=cache_path, data_path=data_path)
            self.cached_sites.append(site)
            self._write_metadata()

    def cleanup_on_exit(self) -> None:
        """Remove transient cache contents while leaving metadata intact."""

        self.stop_server()
        for entry in self.cache_dir.glob("*"):
            try:
                if entry.is_file():
                    entry.unlink()
                else:
                    shutil.rmtree(entry)
            except OSError:
                continue

    # ------------------------------------------------------------------
    # P2P server operations
    # ------------------------------------------------------------------
    def start_server(self, public_ip: str = DEFAULT_PUBLIC_IP, port: int = DEFAULT_PORT) -> None:
        """Start a lightweight TCP server that announces cached sites."""

        if self._server is not None:
            raise RuntimeError("P2P server is already running")

        self.public_ip = public_ip
        bind_host = public_ip
        try:
            socket.inet_aton(bind_host)
        except OSError:
            bind_host = "0.0.0.0"

        try:
            server = _ThreadedTCPServer((bind_host, port), _PeerRequestHandler, self)
        except OSError as exc:
            if bind_host != "0.0.0.0":
                server = _ThreadedTCPServer(("0.0.0.0", port), _PeerRequestHandler, self)
            else:
                raise RuntimeError(f"unable to start P2P server: {exc}") from exc

        self._server = server
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._thread = thread

    def stop_server(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None


__all__ = ["P2PManager", "CachedSite", "DEFAULT_PUBLIC_IP", "DEFAULT_PORT"]

