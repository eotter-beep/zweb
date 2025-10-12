#!/usr/bin/env python3
"""Modern installer for the ZWeb desktop utilities.

The script creates (or reuses) a Python virtual environment, installs the
project requirements, and prints a concise summary with follow-up commands.  It
is intentionally self-contained so that users only need a working Python
interpreter to get started.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable
import venv

PROJECT_ROOT = Path(__file__).resolve().parent
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
DEFAULT_VENV = PROJECT_ROOT / ".venv"


class _Colours:
    """Utility providing ANSI escape sequences when available."""

    _MAP = {
        "reset": "0",
        "title": "95",  # bright magenta
        "step": "94",  # bright blue
        "success": "92",  # bright green
        "warning": "93",  # bright yellow
        "error": "91",  # bright red
    }

    @staticmethod
    def wrap(text: str, role: str) -> str:
        if not sys.stdout.isatty():
            return text
        code = _Colours._MAP.get(role)
        if not code:
            return text
        return f"\033[{code}m{text}\033[0m"


def _print_header() -> None:
    border = _Colours.wrap("═" * 46, "title")
    print(_Colours.wrap("╔" + border + "╗", "title"))
    print(_Colours.wrap("║            ZWeb Desktop Installer            ║", "title"))
    print(_Colours.wrap("╚" + border + "╝", "title"))


def _print_step(message: str) -> None:
    print(f"{_Colours.wrap('➤', 'step')} {message}")


def _print_success(message: str) -> None:
    print(f"{_Colours.wrap('✔', 'success')} {message}")


def _print_warning(message: str) -> None:
    print(f"{_Colours.wrap('⚠', 'warning')} {message}")


def _print_error(message: str) -> None:
    print(f"{_Colours.wrap('✖', 'error')} {message}")


def _venv_python(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def _run_command(command: Iterable[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command {' '.join(command)} failed with exit code {result.returncode}")


def _ensure_venv(venv_path: Path) -> None:
    if venv_path.exists():
        _print_step(f"Reusing virtual environment at {venv_path}")
        return

    _print_step(f"Creating virtual environment at {venv_path}")
    builder = venv.EnvBuilder(with_pip=True)
    builder.create(str(venv_path))


def _install_requirements(python_executable: Path) -> None:
    if not REQUIREMENTS_FILE.exists():
        raise FileNotFoundError("requirements.txt is missing; cannot install dependencies")

    _print_step("Upgrading pip")
    _run_command([str(python_executable), "-m", "pip", "install", "--upgrade", "pip"])

    _print_step("Installing ZWeb requirements")
    _run_command([str(python_executable), "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install ZWeb dependencies into a virtual environment")
    parser.add_argument(
        "--venv",
        default=str(DEFAULT_VENV),
        help="Path to the virtual environment directory (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Create the virtual environment without installing dependencies",
    )
    args = parser.parse_args(argv)

    _print_header()

    venv_path = Path(args.venv).expanduser().resolve()
    try:
        _ensure_venv(venv_path)
    except Exception as exc:  # pragma: no cover - defensive feedback
        _print_error(f"Failed to create virtual environment: {exc}")
        return 1

    python_executable = _venv_python(venv_path)
    if not python_executable.exists():
        _print_warning("Python executable missing from virtual environment; attempting repair")
        try:
            _ensure_venv(venv_path)
        except Exception as exc:  # pragma: no cover - defensive feedback
            _print_error(f"Unable to repair virtual environment: {exc}")
            return 1

    if args.skip_install:
        _print_warning("Skipping dependency installation per user request")
    else:
        try:
            _install_requirements(python_executable)
        except Exception as exc:  # pragma: no cover - defensive feedback
            _print_error(f"Installation failed: {exc}")
            _print_warning("You can rerun the installer after resolving the issue.")
            return 1

    _print_success("ZWeb environment ready")
    print()
    print("Next steps:")
    if os.name == "nt":
        activate_hint = python_executable.parent / "activate.bat"
    else:
        activate_hint = python_executable.parent / "activate"
    print(f"  1. Activate the environment: {activate_hint}")
    print("  2. Launch the browser: python zweb_qt_browser.py")
    print("  3. Use --backend=gtk if PyQt is unavailable on your platform")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
