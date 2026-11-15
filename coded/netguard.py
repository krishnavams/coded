"""Which URLs reach something private: this machine, the local network, a
cloud metadata endpoint, or a local file.

`web_fetch` and the `browser` tool ask before going to such an address
(server-side request forgery: a page or a prompt-injected instruction could
otherwise make coded read `http://169.254.169.254/` or an internal admin
panel), and re-check every redirect. Local development still works: testing
`http://localhost:3000` is one yes, or an allow rule such as
`web_fetch(http://localhost:*)`.

DNS rebinding: checking a name and then letting the HTTP client resolve it
again leaves a gap (the second answer can be 127.0.0.1). `web_fetch`
therefore connects through `PinnedTransport`: each request (redirects
included) resolves once, is judged on those addresses and is sent to the very
address that was judged, with the original name kept for the Host header, TLS
SNI and certificate checks. Answers are never cached across requests.
Limits: behind an HTTP(S) proxy the proxy resolves names, so only the local
answer is checked; the browser resolves names itself (see tools/browser.py).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Callable, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

_INTERNAL_NAMES = {"localhost", "metadata.google.internal", "metadata"}


Resolver = Callable[[str], List[str]]


def _ip_is_internal(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    for embedded in _embedded_v4(addr):
        if _ip_is_internal(str(embedded)):
            return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified
            or (isinstance(addr, ipaddress.IPv4Address) and addr in _SHARED_V4))


# 100.64.0.0/10 (carrier-grade NAT, also some cloud metadata/internal ranges)
# is not "private" to the ipaddress module.
_SHARED_V4 = ipaddress.ip_network("100.64.0.0/10")


def _embedded_v4(addr) -> list:
    """IPv4 addresses hidden in an IPv6 one: ::ffff:a.b.c.d, 6to4, NAT64."""
    if not isinstance(addr, ipaddress.IPv6Address):
        return []
    out = []
    if addr.ipv4_mapped is not None:
        out.append(addr.ipv4_mapped)
    if addr.sixtofour is not None:
        out.append(addr.sixtofour)
    if addr in ipaddress.ip_network("64:ff9b::/96"):
        out.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return out


def _normal_host(host: str) -> str:
    return (host or "").strip("[]").lower().rstrip(".")


def _name_is_internal(host: str) -> bool:
    return host in _INTERNAL_NAMES or host.endswith((".localhost", ".internal", ".local"))


def resolve(host: str) -> List[str]:
    """Every address the name resolves to now (no cache); [] when it does not."""
    host = _normal_host(host)
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return [host]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    out: List[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in out:
            out.append(ip)
    return out


def addresses_reason(host: str, ips: Sequence[str]) -> Optional[str]:
    """Why a host with these resolved addresses is internal, or None."""
    host = _normal_host(host)
    if _name_is_internal(host) or any(_ip_is_internal(ip) for ip in ips):
        return f"an internal address ({host})"
    return None


def host_is_internal(host: str, resolver: Optional[Resolver] = None) -> bool:
    """True when the host names or resolves to a non-public address. A name
    that does not resolve is not internal (the request will fail anyway).
    Resolved every time: a cached answer is what DNS rebinding exploits."""
    host = _normal_host(host)
    if not host:
        return False
    return addresses_reason(host, (resolver or resolve)(host)) is not None


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


class DestinationRefused(PermissionError):
    """A request was not sent: its destination is internal and not approved."""


def pinned_transport(allow: Callable[[str, Optional[str]], bool],
                     resolver: Optional[Resolver] = None, **kwargs):
    """An httpx transport that resolves each request's host once, asks
    `allow(url, reason)` (reason is None for a public destination), and then
    connects to exactly the address it judged — so a second DNS answer
    (rebinding) is never used. The original name stays in the Host header and
    is used for TLS SNI and certificate verification. No keep-alive: a pooled
    connection is keyed by address and must not carry another host's request."""
    import httpx

    resolver = resolver or resolve

    class PinnedTransport(httpx.HTTPTransport):
        def handle_request(self, request):
            host = request.url.host
            ips = resolver(host)
            if not ips:
                raise httpx.ConnectError(f"cannot resolve {host}", request=request)
            reason = addresses_reason(host, ips)
            url = str(request.url)
            if not allow(url, reason):
                raise DestinationRefused(f"not allowed to fetch {url}")
            # Connect to one judged address; prefer IPv4 (wider reachability).
            ip = next((a for a in ips if ":" not in a), ips[0])
            request.url = request.url.copy_with(host=ip)
            if request.url.scheme == "https":
                request.extensions = {**request.extensions, "sni_hostname": host}
            return super().handle_request(request)

    kwargs.setdefault("limits", httpx.Limits(max_keepalive_connections=0))
    return PinnedTransport(**kwargs)


def proxy_for(url: str, env: Optional[dict] = None) -> Optional[str]:
    """The proxy the environment sets for this URL (honouring NO_PROXY), or None."""
    from urllib.parse import urlparse

    env = dict(os.environ if env is None else env)
    parsed = urlparse(url)
    proxies = {k.lower()[:-6]: v for k, v in env.items()
               if k.lower().endswith("_proxy") and k.lower()[:-6] in ("http", "https", "all")
               and v}
    proxy = proxies.get(parsed.scheme) or proxies.get("all")
    if not proxy:
        return None
    no_proxy = env.get("no_proxy") or env.get("NO_PROXY") or ""
    host = _normal_host(parsed.hostname or "")
    for entry in (e.strip().lower() for e in no_proxy.split(",") if e.strip()):
        if entry == "*":
            return None
        try:
            if ipaddress.ip_address(host) in ipaddress.ip_network(entry, strict=False):
                return None
            continue
        except ValueError:
            pass
        entry = entry.lstrip(".")
        if host == entry or host.endswith("." + entry):
            return None
    return proxy
