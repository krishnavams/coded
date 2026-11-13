"""Which URLs reach something private: this machine, the local network, a
cloud metadata endpoint, or a local file.

`web_fetch` and the `browser` tool ask before going to such an address
(server-side request forgery: a page or a prompt-injected instruction could
otherwise make coded read `http://169.254.169.254/` or an internal admin
panel), and re-check every redirect. Local development still works: testing
`http://localhost:3000` is one yes, or an allow rule such as
`web_fetch(http://localhost:*)`.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from functools import lru_cache
from typing import Iterable, Optional
from urllib.parse import urlparse

_INTERNAL_NAMES = {"localhost", "metadata.google.internal", "metadata"}


def _ip_is_internal(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified)


@lru_cache(maxsize=256)
def host_is_internal(host: str) -> bool:
    """True when the host names or resolves to a non-public address. A name
    that does not resolve is not internal (the request will fail anyway)."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return False
    if host in _INTERNAL_NAMES or host.endswith((".localhost", ".internal", ".local")):
        return True
    if _ip_is_internal(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    return any(_ip_is_internal(info[4][0]) for info in infos)


def internal_reason(url: str) -> Optional[str]:
    """Why a URL needs approval ("a local file", "an internal address"), or None."""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return "a local file"
    if parsed.scheme in ("http", "https") and host_is_internal(parsed.hostname or ""):
        return f"an internal address ({parsed.hostname})"
    return None


def file_url_inside(url: str, roots: Iterable[str]) -> bool:
    """A file:// URL that stays inside one of the roots (symlinks resolved)."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return False
    from urllib.request import url2pathname

    path = os.path.realpath(url2pathname(parsed.path))
    for root in roots:
        r = os.path.realpath(os.path.expanduser(root))
        if path == r or path.startswith(r.rstrip(os.sep) + os.sep):
            return True
    return False
