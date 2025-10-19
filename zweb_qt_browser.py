"""Graphical front-end for interacting with the ZWeb DNS helpers via sockets.

The original application only supported PyQt which made it difficult to run in
environments where Qt bindings are unavailable.  The module now prefers PyQt5
but falls back to GTK (via PyGObject) when Qt cannot be imported.  The public
``main`` entry point accepts a ``--backend`` argument that allows callers to
force a particular toolkit while retaining the automatic behaviour by default.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import zweb_p2p

try:  # pragma: no cover - import guard for environments without PyQt
    from PyQt5 import QtCore, QtGui, QtWidgets
except ImportError as exc:  # pragma: no cover - import guard for environments without PyQt
    QtCore = None  # type: ignore[assignment]
    QtGui = None  # type: ignore[assignment]
    QtWidgets = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

try:  # pragma: no cover - import guard for environments without GTK
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk  # type: ignore[attr-defined]
except (ImportError, ValueError) as exc:  # pragma: no cover - import guard
    Gtk = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]
    _GTK_IMPORT_ERROR = exc
else:
    _GTK_IMPORT_ERROR = None

import zweb_socket_server

HOST = zweb_socket_server.HOST
PORT = zweb_socket_server.PORT

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _parse_host_input(raw: str, default_port: int) -> Tuple[str, int]:
    """Return ``(host, port)`` from the user provided ``raw`` string."""

    candidate = raw.strip()
    if not candidate:
        raise ValueError("server address is required")

    host = candidate
    port = default_port

    if candidate.startswith("[") and "]" in candidate:
        closing = candidate.index("]")
        host = candidate[1:closing].strip()
        remainder = candidate[closing + 1 :].strip()
        if remainder.startswith(":") and remainder[1:]:
            try:
                port = int(remainder[1:])
            except ValueError as exc:  # pragma: no cover - defensive guard
                raise ValueError("invalid port specified") from exc
    elif ":" in candidate and candidate.count(":") == 1:
        host_part, port_part = candidate.split(":", 1)
        host = host_part.strip()
        if port_part.strip():
            try:
                port = int(port_part.strip())
            except ValueError as exc:  # pragma: no cover - defensive guard
                raise ValueError("invalid port specified") from exc
    else:
        host = candidate

    host = host or "127.0.0.1"
    return host, port


def _is_local_address(host: str) -> bool:
    return host in _LOCAL_HOSTS


class LookupError(RuntimeError):
    """Raised when the socket server cannot satisfy a lookup request."""

    def __init__(self, message: str, *, error_page: Optional[str] = None) -> None:
        super().__init__(message)
        self.error_page = error_page


def _read_response(sock: socket.socket) -> dict[str, str]:
    data = bytearray()
    while True:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data.extend(chunk)
        if data.endswith(b"\n"):
            break
    if not data:
        raise LookupError("no response received from server")
    try:
        payload = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive only
        raise LookupError("server returned invalid JSON") from exc
    return payload


def _ping_server(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"PING\n")
            payload = _read_response(sock)
    except OSError:
        return False
    except LookupError:
        return False
    return payload.get("status") == "ok"


_server_lock = threading.Lock()
_server_thread: Optional[threading.Thread] = None


def ensure_server_running(host: str, port: int) -> None:
    """Start ``zweb_socket_server`` in a background thread if required."""

    global _server_thread

    if _ping_server(host, port):
        return

    with _server_lock:
        if _server_thread and _server_thread.is_alive():
            # The existing thread might just be starting up.
            pass
        else:
            _server_thread = threading.Thread(
                target=zweb_socket_server.serve,
                kwargs={"host": host, "port": port},
                daemon=True,
            )
            _server_thread.start()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _ping_server(host, port):
            return
        time.sleep(0.1)
    raise RuntimeError("unable to start the ZWeb socket server")


@dataclass
class LookupResult:
    hostname: str
    zone: str
    node: str
    name: str


class LookupClient:
    """Tiny client used by the GUI to fetch DNS information."""

    def __init__(self, host: str, port: int, timeout: float = 2.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._domain_server: Optional[str] = None

    def set_endpoint(self, host: str, port: int) -> None:
        self._host = host
        self._port = port

    def get_endpoint(self) -> Tuple[str, int]:
        return self._host, self._port

    def _send_command(self, command: str) -> dict[str, str]:
        try:
            with socket.create_connection((self._host, self._port), timeout=self._timeout) as sock:
                sock.sendall(command.encode("utf-8") + b"\n")
                payload = _read_response(sock)
        except OSError as exc:
            raise LookupError(f"unable to contact server: {exc}") from exc
        return payload

    def lookup(self, query: str) -> LookupResult:
        request = query.strip()
        if not request:
            raise LookupError("please provide a domain or URL")

        payload = self._send_command(request)

        if payload.get("status") != "ok":
            message = payload.get("message", "lookup failed")
            raise LookupError(message, error_page=payload.get("error_page"))

        self._domain_server = payload.get("domain_server")

        return LookupResult(
            hostname=payload.get("hostname", ""),
            zone=payload.get("zone", ""),
            node=payload.get("node", ""),
            name=payload.get("name", ""),
        )

    def list_servers(self) -> tuple[list[dict[str, str]], Optional[str]]:
        payload = self._send_command("SERVERS")

        if payload.get("status") != "ok":
            message = payload.get("message", "unable to list servers")
            raise LookupError(message, error_page=payload.get("error_page"))

        servers = payload.get("servers")
        if not isinstance(servers, list):
            servers = []

        domain_server = payload.get("domain_server")
        if isinstance(domain_server, str):
            self._domain_server = domain_server

        normalised: list[dict[str, str]] = []
        for entry in servers:
            if not isinstance(entry, dict):
                continue
            normalised.append({
                "name": str(entry.get("name", "")),
                "address": str(entry.get("address", "")),
                "description": str(entry.get("description", "")),
            })

        return normalised, self._domain_server


if QtCore and QtGui:  # pragma: no branch - depends on import guard

    class LookupWorker(QtCore.QObject):
        finished = QtCore.pyqtSignal(LookupResult)
        failed = QtCore.pyqtSignal(str, object)

        def __init__(self, client: LookupClient, query: str) -> None:
            super().__init__()
            self._client = client
            self._query = query

        @QtCore.pyqtSlot()
        def run(self) -> None:
            try:
                result = self._client.lookup(self._query)
            except LookupError as exc:
                self.failed.emit(str(exc), getattr(exc, "error_page", None))
            else:
                self.finished.emit(result)


    class MainWindow(QtWidgets.QWidget):
        def __init__(self, client: LookupClient, p2p_manager: zweb_p2p.P2PManager) -> None:
            super().__init__()

            self._client = client
            self._p2p_manager = p2p_manager
            self._current_alias: Optional[str] = None
            self._last_query: Optional[str] = None

            self.setWindowTitle("ZWeb Browser")
            self.setMinimumWidth(520)

            self._input = QtWidgets.QLineEdit(self)
            self._input.setPlaceholderText("Enter a domain or URL")

            self._lookup_button = QtWidgets.QPushButton("Lookup", self)
            self._lookup_button.clicked.connect(self._trigger_lookup)

            form_layout = QtWidgets.QFormLayout()
            self._hostname_label = QtWidgets.QLabel("–", self)
            self._zone_label = QtWidgets.QLabel("–", self)
            self._node_label = QtWidgets.QLabel("–", self)
            self._name_label = QtWidgets.QLabel("–", self)

            for label in (self._hostname_label, self._zone_label, self._node_label, self._name_label):
                label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

            form_layout.addRow("Hostname:", self._hostname_label)
            form_layout.addRow("Zone (zwb):", self._zone_label)
            form_layout.addRow("Node:", self._node_label)
            form_layout.addRow("Full name:", self._name_label)

            self._download_button = QtWidgets.QPushButton("Download & Share", self)
            self._download_button.setEnabled(False)
            self._download_button.clicked.connect(self._download_current_site)

            endpoint_host, endpoint_port = self._client.get_endpoint()
            self._dns_host_input = QtWidgets.QLineEdit(self)
            self._dns_host_input.setPlaceholderText("Domain server (e.g., 1.1.1.1 or 127.0.0.1)")
            self._dns_host_input.setText(f"{endpoint_host}:{endpoint_port}")

            self._apply_dns_button = QtWidgets.QPushButton("Apply DNS Server", self)
            self._apply_dns_button.clicked.connect(self._apply_dns_server)

            self._refresh_servers_button = QtWidgets.QPushButton("List Servers", self)
            self._refresh_servers_button.clicked.connect(self._refresh_server_list)

            dns_layout = QtWidgets.QHBoxLayout()
            dns_layout.addWidget(self._dns_host_input)
            dns_layout.addWidget(self._apply_dns_button)
            dns_layout.addWidget(self._refresh_servers_button)

            self._server_directory = QtWidgets.QPlainTextEdit(self)
            self._server_directory.setReadOnly(True)
            self._server_directory.setPlaceholderText("Server directory not loaded")
            self._server_directory.setMaximumHeight(120)

            self._server_ip_input = QtWidgets.QLineEdit(self)
            self._server_ip_input.setPlaceholderText("P2P public IP address")
            self._server_ip_input.setText(zweb_p2p.DEFAULT_PUBLIC_IP)

            self._start_server_button = QtWidgets.QPushButton("Start P2P Server", self)
            self._start_server_button.clicked.connect(self._start_p2p_server)

            self._stop_server_button = QtWidgets.QPushButton("Stop P2P Server", self)
            self._stop_server_button.clicked.connect(self._stop_p2p_server)
            self._stop_server_button.setEnabled(False)

            server_layout = QtWidgets.QHBoxLayout()
            server_layout.addWidget(self._server_ip_input)
            server_layout.addWidget(self._start_server_button)
            server_layout.addWidget(self._stop_server_button)

            self._error_page_view = QtWidgets.QTextBrowser(self)
            self._error_page_view.setOpenExternalLinks(False)
            self._error_page_view.setVisible(False)
            self._error_page_view.setMaximumHeight(220)

            self._p2p_status = QtWidgets.QLabel("P2P server stopped", self)

            self._status = QtWidgets.QLabel("Ready", self)

            layout = QtWidgets.QVBoxLayout(self)
            layout.addWidget(self._input)
            layout.addWidget(self._lookup_button)
            layout.addWidget(self._download_button)
            layout.addLayout(dns_layout)
            layout.addWidget(self._server_directory)
            layout.addLayout(form_layout)
            layout.addWidget(self._error_page_view)
            layout.addLayout(server_layout)
            layout.addWidget(self._p2p_status)
            layout.addWidget(self._status)
            layout.addStretch()

        def _trigger_lookup(self) -> None:
            query = self._input.text()
            if not query.strip():
                self._status.setText("Please enter a domain or URL")
                return

            self._lookup_button.setEnabled(False)
            self._download_button.setEnabled(False)
            self._status.setText("Looking up…")
            self._last_query = query
            self._error_page_view.clear()
            self._error_page_view.setVisible(False)

            thread = QtCore.QThread(self)
            worker = LookupWorker(self._client, query)
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.finished.connect(self._handle_result)
            worker.failed.connect(self._handle_error)
            worker.finished.connect(thread.quit)
            worker.failed.connect(lambda *_: thread.quit())
            worker.finished.connect(worker.deleteLater)
            worker.failed.connect(lambda *_: worker.deleteLater())
            thread.finished.connect(thread.deleteLater)
            thread.start()

        def _handle_result(self, result: LookupResult) -> None:
            self._hostname_label.setText(result.hostname or "–")
            self._zone_label.setText(result.zone or "–")
            self._node_label.setText(result.node or "–")
            self._name_label.setText(result.name or "–")
            self._status.setText("Lookup successful")
            self._lookup_button.setEnabled(True)
            self._download_button.setEnabled(True)
            self._error_page_view.clear()
            self._error_page_view.setVisible(False)

            previous_alias = self._current_alias
            self._current_alias = result.name or result.zone or result.hostname
            if previous_alias and previous_alias != self._current_alias:
                self._p2p_manager.mark_site_cached(previous_alias)

        def _handle_error(self, message: str, error_page: object) -> None:
            self._status.setText(message)
            self._lookup_button.setEnabled(True)
            self._download_button.setEnabled(False)
            if isinstance(error_page, str) and error_page.strip():
                self._error_page_view.setHtml(error_page)
                self._error_page_view.setVisible(True)
            else:
                self._error_page_view.clear()
                self._error_page_view.setVisible(False)

        def _download_current_site(self) -> None:
            if not self._current_alias:
                self._status.setText("No lookup data available")
                return

            source = self._last_query or self._hostname_label.text()
            alias = self._current_alias
            hostname = self._hostname_label.text()

            if not source.strip():
                self._status.setText("No source URL to download")
                return

            try:
                site = self._p2p_manager.install_site(source.strip(), alias, hostname.strip())
            except RuntimeError as exc:
                self._status.setText(str(exc))
            else:
                self._status.setText(f"Cached site at {site.cache_path}")

        def _apply_dns_server(self) -> None:
            raw_value = self._dns_host_input.text()
            try:
                host, port = _parse_host_input(raw_value, PORT)
            except ValueError as exc:
                self._status.setText(str(exc))
                return

            self._client.set_endpoint(host, port)
            self._dns_host_input.setText(f"{host}:{port}")

            if _is_local_address(host):
                try:
                    ensure_server_running(host, port)
                except Exception as exc:  # pragma: no cover - defensive only
                    self._status.setText(f"Unable to start DNS server: {exc}")
                    return

            self._status.setText(f"Using DNS server {host}:{port}")
            self._refresh_server_list()

        def _refresh_server_list(self) -> None:
            try:
                servers, domain_server = self._client.list_servers()
            except LookupError as exc:
                self._server_directory.setPlainText(str(exc))
                if exc.error_page:
                    self._error_page_view.setHtml(exc.error_page)
                    self._error_page_view.setVisible(True)
                else:
                    self._error_page_view.clear()
                    self._error_page_view.setVisible(False)
                self._status.setText(str(exc))
                return

            lines = []
            if domain_server:
                lines.append(f"Domain server: {domain_server}")

            for entry in servers:
                name = entry.get("name", "Server").strip() or "Server"
                address = entry.get("address", "").strip()
                description = entry.get("description", "").strip()

                if address:
                    line = f"{name}: {address}"
                else:
                    line = name
                if description:
                    line = f"{line} — {description}"
                lines.append(line)

            if not lines:
                lines.append("No servers reported")

            self._server_directory.setPlainText("\n".join(lines))
            self._status.setText("Server directory refreshed")
            self._error_page_view.clear()
            self._error_page_view.setVisible(False)

        def _start_p2p_server(self) -> None:
            ip_address = self._server_ip_input.text().strip() or zweb_p2p.DEFAULT_PUBLIC_IP
            try:
                self._p2p_manager.start_server(ip_address)
            except RuntimeError as exc:
                self._p2p_status.setText(str(exc))
                return

            self._p2p_status.setText(f"P2P server broadcasting on {ip_address}:{zweb_p2p.DEFAULT_PORT}")
            self._start_server_button.setEnabled(False)
            self._stop_server_button.setEnabled(True)

        def _stop_p2p_server(self) -> None:
            self._p2p_manager.stop_server()
            self._p2p_status.setText("P2P server stopped")
            self._start_server_button.setEnabled(True)
            self._stop_server_button.setEnabled(False)

        def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # type: ignore[override]
            self._p2p_manager.stop_server()
            self._p2p_manager.cleanup_on_exit()
            super().closeEvent(event)


if Gtk:  # pragma: no branch - depends on import guard

    class GtkMainWindow(Gtk.Window):
        """GTK implementation mirroring the PyQt interface."""

        def __init__(self, client: LookupClient, p2p_manager: zweb_p2p.P2PManager) -> None:
            super().__init__(title="ZWeb Browser")

            self._client = client
            self._p2p_manager = p2p_manager
            self._current_alias: Optional[str] = None
            self._last_query: Optional[str] = None

            self.set_border_width(12)
            self.set_default_size(540, 360)

            container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            self.add(container)

            entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            container.pack_start(entry_box, False, False, 0)

            self._input = Gtk.Entry()
            self._input.set_placeholder_text("Enter a domain or URL")
            entry_box.pack_start(self._input, True, True, 0)

            self._lookup_button = Gtk.Button(label="Lookup")
            self._lookup_button.connect("clicked", self._trigger_lookup)
            entry_box.pack_start(self._lookup_button, False, False, 0)

            self._download_button = Gtk.Button(label="Download & Share")
            self._download_button.set_sensitive(False)
            self._download_button.connect("clicked", self._download_current_site)
            container.pack_start(self._download_button, False, False, 0)

            endpoint_host, endpoint_port = self._client.get_endpoint()
            dns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            container.pack_start(dns_box, False, False, 0)

            self._dns_host_input = Gtk.Entry()
            self._dns_host_input.set_placeholder_text("Domain server (e.g., 1.1.1.1 or 127.0.0.1)")
            self._dns_host_input.set_text(f"{endpoint_host}:{endpoint_port}")
            dns_box.pack_start(self._dns_host_input, True, True, 0)

            self._apply_dns_button = Gtk.Button(label="Apply DNS Server")
            self._apply_dns_button.connect("clicked", self._apply_dns_server)
            dns_box.pack_start(self._apply_dns_button, False, False, 0)

            self._refresh_servers_button = Gtk.Button(label="List Servers")
            self._refresh_servers_button.connect("clicked", self._refresh_server_list)
            dns_box.pack_start(self._refresh_servers_button, False, False, 0)

            grid = Gtk.Grid(row_spacing=6, column_spacing=12)
            container.pack_start(grid, False, False, 0)

            self._hostname_value = Gtk.Label(label="–")
            self._zone_value = Gtk.Label(label="–")
            self._node_value = Gtk.Label(label="–")
            self._name_value = Gtk.Label(label="–")

            for label in (self._hostname_value, self._zone_value, self._node_value, self._name_value):
                label.set_selectable(True)

            labels = [
                ("Hostname:", self._hostname_value),
                ("Zone (zwb):", self._zone_value),
                ("Node:", self._node_value),
                ("Full name:", self._name_value),
            ]

            for row, (title, widget) in enumerate(labels):
                grid.attach(Gtk.Label(label=title, xalign=0), 0, row, 1, 1)
                grid.attach(widget, 1, row, 1, 1)

            self._server_directory = Gtk.TextView()
            self._server_directory.set_editable(False)
            self._server_directory.set_cursor_visible(False)
            self._server_directory.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self._server_directory.set_size_request(-1, 100)
            container.pack_start(self._server_directory, False, False, 0)
            self._server_directory.get_buffer().set_text("Server directory not loaded")

            p2p_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            container.pack_start(p2p_box, False, False, 0)

            self._server_ip_input = Gtk.Entry()
            self._server_ip_input.set_placeholder_text("P2P public IP address")
            self._server_ip_input.set_text(zweb_p2p.DEFAULT_PUBLIC_IP)
            p2p_box.pack_start(self._server_ip_input, True, True, 0)

            self._start_server_button = Gtk.Button(label="Start P2P Server")
            self._start_server_button.connect("clicked", self._start_p2p_server)
            p2p_box.pack_start(self._start_server_button, False, False, 0)

            self._stop_server_button = Gtk.Button(label="Stop P2P Server")
            self._stop_server_button.connect("clicked", self._stop_p2p_server)
            self._stop_server_button.set_sensitive(False)
            p2p_box.pack_start(self._stop_server_button, False, False, 0)

            self._p2p_status = Gtk.Label(label="P2P server stopped")
            self._status = Gtk.Label(label="Ready")

            self._error_page_view = Gtk.TextView()
            self._error_page_view.set_editable(False)
            self._error_page_view.set_cursor_visible(False)
            self._error_page_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            self._error_page_view.set_size_request(-1, 160)
            container.pack_start(self._error_page_view, False, False, 0)
            self._error_page_view.hide()

            container.pack_start(self._p2p_status, False, False, 0)
            container.pack_start(self._status, False, False, 0)
            container.pack_start(Gtk.Box(), True, True, 0)

        def _trigger_lookup(self, *_: object) -> None:
            query = self._input.get_text()
            if not query.strip():
                self._status.set_text("Please enter a domain or URL")
                return

            self._lookup_button.set_sensitive(False)
            self._download_button.set_sensitive(False)
            self._status.set_text("Looking up…")
            self._last_query = query
            self._set_error_page(None)

            thread = threading.Thread(target=self._lookup_in_background, args=(query,), daemon=True)
            thread.start()

        def _lookup_in_background(self, query: str) -> None:
            try:
                result = self._client.lookup(query)
            except LookupError as exc:
                GLib.idle_add(self._handle_error, str(exc), exc.error_page)
            else:
                GLib.idle_add(self._handle_result, result)

        def _handle_result(self, result: LookupResult) -> None:
            self._hostname_value.set_text(result.hostname or "–")
            self._zone_value.set_text(result.zone or "–")
            self._node_value.set_text(result.node or "–")
            self._name_value.set_text(result.name or "–")
            self._status.set_text("Lookup successful")
            self._lookup_button.set_sensitive(True)
            self._download_button.set_sensitive(True)
            self._set_error_page(None)

            previous_alias = self._current_alias
            self._current_alias = result.name or result.zone or result.hostname
            if previous_alias and previous_alias != self._current_alias:
                self._p2p_manager.mark_site_cached(previous_alias)

        def _handle_error(self, message: str, error_page: Optional[str]) -> None:
            self._status.set_text(message)
            self._lookup_button.set_sensitive(True)
            self._download_button.set_sensitive(False)
            self._set_error_page(error_page)

        def _download_current_site(self, *_: object) -> None:
            if not self._current_alias:
                self._status.set_text("No lookup data available")
                return

            source = self._last_query or self._hostname_value.get_text()
            alias = self._current_alias
            hostname = self._hostname_value.get_text()

            if not source.strip():
                self._status.set_text("No source URL to download")
                return

            try:
                site = self._p2p_manager.install_site(source.strip(), alias, hostname.strip())
            except RuntimeError as exc:
                self._status.set_text(str(exc))
            else:
                self._status.set_text(f"Cached site at {site.cache_path}")

        def _set_error_page(self, page: Optional[str]) -> None:
            buffer = self._error_page_view.get_buffer()
            if page and page.strip():
                buffer.set_text(page)
                self._error_page_view.show()
            else:
                buffer.set_text("")
                self._error_page_view.hide()

        def _apply_dns_server(self, *_: object) -> None:
            raw_value = self._dns_host_input.get_text()
            try:
                host, port = _parse_host_input(raw_value, PORT)
            except ValueError as exc:
                self._status.set_text(str(exc))
                return

            self._client.set_endpoint(host, port)
            self._dns_host_input.set_text(f"{host}:{port}")

            if _is_local_address(host):
                try:
                    ensure_server_running(host, port)
                except Exception as exc:  # pragma: no cover - defensive only
                    self._status.set_text(f"Unable to start DNS server: {exc}")
                    return

            self._status.set_text(f"Using DNS server {host}:{port}")
            self._refresh_server_list()

        def _refresh_server_list(self, *_: object) -> None:
            try:
                servers, domain_server = self._client.list_servers()
            except LookupError as exc:
                self._server_directory.get_buffer().set_text(str(exc))
                self._set_error_page(exc.error_page)
                self._status.set_text(str(exc))
                return

            lines: list[str] = []
            if domain_server:
                lines.append(f"Domain server: {domain_server}")

            for entry in servers:
                name = entry.get("name", "Server").strip() or "Server"
                address = entry.get("address", "").strip()
                description = entry.get("description", "").strip()

                if address:
                    line = f"{name}: {address}"
                else:
                    line = name
                if description:
                    line = f"{line} — {description}"
                lines.append(line)

            if not lines:
                lines.append("No servers reported")

            self._server_directory.get_buffer().set_text("\n".join(lines))
            self._status.set_text("Server directory refreshed")
            self._set_error_page(None)

        def _start_p2p_server(self, *_: object) -> None:
            ip_address = self._server_ip_input.get_text().strip() or zweb_p2p.DEFAULT_PUBLIC_IP
            try:
                self._p2p_manager.start_server(ip_address)
            except RuntimeError as exc:
                self._p2p_status.set_text(str(exc))
                return

            self._p2p_status.set_text(
                f"P2P server broadcasting on {ip_address}:{zweb_p2p.DEFAULT_PORT}"
            )
            self._start_server_button.set_sensitive(False)
            self._stop_server_button.set_sensitive(True)

        def _stop_p2p_server(self, *_: object) -> None:
            self._p2p_manager.stop_server()
            self._p2p_status.set_text("P2P server stopped")
            self._start_server_button.set_sensitive(True)
            self._stop_server_button.set_sensitive(False)

        def perform_shutdown(self) -> None:
            self._p2p_manager.stop_server()
            self._p2p_manager.cleanup_on_exit()


def _run_with_qt(args: argparse.Namespace) -> int:
    if _IMPORT_ERROR is not None or QtWidgets is None:
        message = "PyQt5 is required but not available"
        if _IMPORT_ERROR is not None:
            message = f"{message}: {_IMPORT_ERROR}"
        print(message, file=sys.stderr)
        return 1

    if not args.no_server and _is_local_address(args.host):
        try:
            ensure_server_running(args.host, args.port)
        except Exception as exc:  # pragma: no cover - defensive user feedback
            print(f"Failed to start socket server: {exc}", file=sys.stderr)
            return 1

    app = QtWidgets.QApplication(sys.argv)
    client = LookupClient(args.host, args.port)
    p2p_manager = zweb_p2p.P2PManager()
    app.aboutToQuit.connect(p2p_manager.cleanup_on_exit)  # type: ignore[attr-defined]
    window = MainWindow(client, p2p_manager)
    window.show()
    return app.exec_()


def _run_with_gtk(args: argparse.Namespace) -> int:
    if Gtk is None or _GTK_IMPORT_ERROR is not None:
        message = "GTK (PyGObject) is required but not available"
        if _GTK_IMPORT_ERROR is not None:
            message = f"{message}: {_GTK_IMPORT_ERROR}"
        print(message, file=sys.stderr)
        return 1

    if not args.no_server and _is_local_address(args.host):
        try:
            ensure_server_running(args.host, args.port)
        except Exception as exc:  # pragma: no cover - defensive user feedback
            print(f"Failed to start socket server: {exc}", file=sys.stderr)
            return 1

    client = LookupClient(args.host, args.port)
    p2p_manager = zweb_p2p.P2PManager()
    window = GtkMainWindow(client, p2p_manager)

    def _on_destroy(*_: object) -> None:
        window.perform_shutdown()
        Gtk.main_quit()

    window.connect("destroy", _on_destroy)
    window.show_all()
    Gtk.main()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ZWeb graphical browser")
    parser.add_argument("--host", default=HOST, help="Socket server host (default: %(default)s)")
    parser.add_argument("--port", type=int, default=PORT, help="Socket server port (default: %(default)s)")
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Do not auto-start the bundled socket server",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "qt", "gtk"],
        default="auto",
        help="Preferred UI backend (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    if args.backend == "qt":
        return _run_with_qt(args)
    if args.backend == "gtk":
        return _run_with_gtk(args)

    if QtWidgets is not None and _IMPORT_ERROR is None:
        return _run_with_qt(args)
    if Gtk is not None and _GTK_IMPORT_ERROR is None:
        return _run_with_gtk(args)

    print("No supported graphical backend is available (PyQt5 or GTK required)", file=sys.stderr)
    if _IMPORT_ERROR is not None:
        print(f"PyQt5 error: {_IMPORT_ERROR}", file=sys.stderr)
    if _GTK_IMPORT_ERROR is not None:
        print(f"GTK error: {_GTK_IMPORT_ERROR}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
