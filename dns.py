"""Simple helpers for working with DNS like data.

The original implementation attempted to import functions from a non-existent
``dns`` package and call them as if they were regular functions.  Because this
module is itself called ``dns`` that resulted in the script importing itself
recursively, prompting for input twice and eventually crashing with attribute
errors.  This module now provides the small helpers that were expected in the
original script without relying on external packages.

The helpers understand conventional DNS hostnames *and* GitHub Pages
addresses.  GitHub Pages hosts follow the ``<owner>.github.io`` convention and
optionally expose projects via ``/<project>`` paths.  When such an address is
encountered we surface a ``.zwb`` alias so that ``hello.github.io`` becomes
``hello.zwb`` and ``hello.github.io/hi`` becomes ``hi.zwb``.

The module exposes three functions:

``zone(domain)``
    Return the zone part of a fully qualified domain name and replace the
    extension with ``.zwb``.  For a hostname such as ``www.example.com`` this
    now returns ``example.zwb``.  GitHub Pages hosts resolve to their ``.zwb``
    alias instead.

``node(domain)``
    Return the node (the labels that precede the zone).  For
    ``www.example.com`` this returns ``www``.  GitHub Pages project URLs report
    the project owner in this field so that ``hello.github.io/hi`` surfaces the
    node ``hello`` with the ``.zwb`` zone ``hi.zwb``.

``name(domain, suffix='zwb')``
    Build a new fully qualified domain name by replacing the existing
    extension with ``suffix``.  The default suffix keeps compatibility with the
    original script which tried to build a name ending in ``.zwb``.  GitHub
    Pages addresses prefer their ``.zwb`` alias regardless of ``suffix``.

When the module is executed as a script it reads a value from standard input,
normalises it into a hostname and prints the three values described above.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import List, Tuple
from urllib.parse import urlparse


@dataclass(frozen=True)
class DomainParts:
    """Container describing a parsed domain."""

    hostname: str
    zone: str
    node: str
    name: str


_GITHUB_PAGES_SUFFIX = ".github.io"


def _parse_input(raw: str) -> Tuple[str, List[str]]:
    """Return the hostname and path components from ``raw``.

    ``raw`` can either be a bare hostname (``example.com``) or a URL
    (``https://example.com``).  A :class:`ValueError` is raised when the input
    is empty or does not contain a valid hostname.  The path is returned as a
    list of segments with empty segments removed.
    """

    candidate = raw.strip()
    if not candidate:
        raise ValueError("a hostname or URL is required")

    if "://" not in candidate:
        candidate = f"http://{candidate}"

    parsed = urlparse(candidate)
    if not parsed.hostname:
        raise ValueError(f"unable to determine hostname from '{raw}'")

    hostname = parsed.hostname.rstrip(".")
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    return hostname, path_segments


def _normalise_hostname(raw: str) -> str:
    """Extract the hostname component from ``raw``."""

    hostname, _ = _parse_input(raw)
    return hostname


def _sanitise_label(label: str) -> str:
    """Convert ``label`` into a DNS-friendly token."""

    cleaned: List[str] = []
    previous_dash = False
    for character in label.lower():
        if character.isalnum():
            cleaned.append(character)
            previous_dash = False
        else:
            if not previous_dash:
                cleaned.append("-")
            previous_dash = True
    result = "".join(cleaned).strip("-")
    return result


def _github_pages_alias(hostname: str, path_segments: List[str], suffix: str) -> str | None:
    """Return the ``.zwb`` alias for GitHub Pages hosts."""

    if not hostname.endswith(_GITHUB_PAGES_SUFFIX):
        return None

    base = hostname[: -len(_GITHUB_PAGES_SUFFIX)].strip(".")
    label_source = path_segments[0] if path_segments else base
    suffix = suffix.strip(".")
    if not suffix:
        raise ValueError("suffix must not be empty")

    candidate = _sanitise_label(label_source)
    if not candidate:
        return None
    return f"{candidate}.{suffix}"


def _split_hostname_and_path(raw: str) -> Tuple[str, List[str]]:
    """Best-effort helper returning hostname and path segments."""

    try:
        return _parse_input(raw)
    except ValueError:
        hostname = raw.strip().rstrip(".")
        return hostname, []


def _replace_suffix(domain: str, suffix: str) -> str:
    """Replace the final label of ``domain`` with ``suffix``.

    When the input only contains a single label the suffix is appended instead
    so that ``localhost`` becomes ``localhost.zwb``.  Trailing dots in the
    inputs are ignored to avoid generating empty labels.
    """

    labels = [label for label in domain.split(".") if label]
    if not labels:
        raise ValueError("domain must not be empty")

    suffix = suffix.strip(".")
    if not suffix:
        raise ValueError("suffix must not be empty")

    if len(labels) == 1:
        return f"{labels[0]}.{suffix}"
    return ".".join(labels[:-1] + [suffix])


def zone(domain: str) -> str:
    """Return the zone (typically the registered domain) using ``.zwb``.

    For ``www.example.com`` this now returns ``example.zwb``.  When no zone can
    be determined the single label is still suffixed with ``.zwb``.
    """

    hostname, path_segments = _split_hostname_and_path(domain)
    if not hostname:
        return ""

    alias = _github_pages_alias(hostname, path_segments, "zwb")
    if alias:
        return alias

    labels = [label for label in hostname.split(".") if label]
    if not labels:
        return ""
    if len(labels) < 2:
        return _replace_suffix(labels[0], "zwb")
    return _replace_suffix(".".join(labels[-2:]), "zwb")


def node(domain: str) -> str:
    """Return the node portion that precedes the zone.

    For ``mail.internal.example.com`` this returns ``mail.internal``.  When the
    domain consists of a single label we simply return that label.
    """

    hostname, path_segments = _split_hostname_and_path(domain)
    if not hostname:
        return ""

    alias = _github_pages_alias(hostname, path_segments, "zwb")
    if alias:
        if path_segments:
            owner = _sanitise_label(hostname[: -len(_GITHUB_PAGES_SUFFIX)].strip("."))
            return owner
        return ""

    labels = [label for label in hostname.split(".") if label]
    if len(labels) <= 2:
        return labels[0] if labels else ""
    return ".".join(labels[:-2])


def name(domain: str, suffix: str = "zwb") -> str:
    """Construct a new domain name by replacing its extension with ``suffix``."""

    hostname, path_segments = _split_hostname_and_path(domain)
    if not hostname:
        raise ValueError("domain must not be empty")

    alias = _github_pages_alias(hostname, path_segments, suffix)
    if alias:
        return alias

    hostname = hostname.rstrip(".")
    return _replace_suffix(hostname, suffix)


def _describe_uncached(raw: str) -> DomainParts:
    """Internal helper implementing :func:`describe` without caching."""

    hostname, path_segments = _split_hostname_and_path(raw)
    if not hostname:
        raise ValueError("a hostname or URL is required")

    alias = _github_pages_alias(hostname, path_segments, "zwb")
    if alias:
        owner = _sanitise_label(hostname[: -len(_GITHUB_PAGES_SUFFIX)].strip("."))
        node_value = owner if path_segments else ""
        return DomainParts(hostname=hostname, zone=alias, node=node_value, name=alias)

    zone_value = zone(hostname)
    node_value = node(hostname)
    return DomainParts(hostname=hostname, zone=zone_value, node=node_value, name=name(hostname))


@lru_cache(maxsize=512)
def describe(raw: str) -> DomainParts:
    """Parse ``raw`` and describe its DNS components."""

    return _describe_uncached(raw)


def build_error_page(message: str, request: str) -> str:
    """Return a lightweight HTML page describing a lookup failure."""

    safe_request = request.strip() or "(empty request)"
    safe_message = message.strip() or "Lookup failed"
    return (
        "<html><head><title>DNS Lookup Error</title>"
        "<style>body{font-family:Arial,Helvetica,sans-serif;margin:2em;}"
        "h1{color:#b00;}code{background:#f5f5f5;padding:0.2em 0.4em;}"
        "</style></head><body>"
        "<h1>Domain lookup failed</h1>"
        f"<p>The DNS helper was unable to resolve <code>{safe_request}</code>.</p>"
        f"<p><strong>Reason:</strong> {safe_message}</p>"
        "<p>Please verify the address and try again or choose a different server.</p>"
        "</body></html>"
    )


def main() -> None:
    """Entry-point used when executing the module as a script."""

    raw = input()
    parts = describe(raw)
    print(parts.zone)
    print(parts.node)
    print(parts.name)


if __name__ == "__main__":
    main()
