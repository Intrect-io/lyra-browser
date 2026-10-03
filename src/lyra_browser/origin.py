"""Origins — the unit the permission layer is keyed on.

The gate used to compare bare hostnames, which quietly let through every URL
that has none. ``urlparse("file:///etc/passwd").hostname`` is ``None``, so
``target_host or ""`` produced ``""``, and the guard read ``if target_host and
...`` — falsy, so **no gate ran at all**. The same held for ``data:``,
``javascript:`` and ``about:``: the less ordinary the scheme, the more freedom
it got. Comparing full origins closes that, and folding scheme-less URLs into an
explicitly opaque origin turns them into deny targets instead of invisible ones.

Ports are normalised so ``https://x.com`` and ``https://x.com:443`` are one
origin, and scheme is part of the identity so an ``https -> http`` downgrade
counts as leaving the site.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit, urlunsplit

# Ports the browser omits from an origin string.
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}

# Schemes that carry no authority. A document from one of these is local
# (``file``), agent-authored (``data``/``blob``), browser-internal (``about``,
# ``chrome``), or not a document at all (``javascript``). None of them can be
# "the site the user is on", so they never count as same-site — not even with
# each other.
_OPAQUE_SCHEMES = frozenset(
    {
        "about",
        "blob",
        "chrome",
        "chrome-error",
        "chrome-extension",
        "data",
        "devtools",
        "file",
        "filesystem",
        "javascript",
        "view-source",
        "vbscript",
    }
)


@dataclass(frozen=True, slots=True)
class Origin:
    """A ``(scheme, host, port)`` triple, or an opaque origin when there is no host."""

    scheme: str
    host: str
    port: int | None

    @property
    def is_opaque(self) -> bool:
        """True when this URL has no authority to attribute a document to."""
        return not self.host

    def same_site_as(self, other: Origin) -> bool:
        """Whether staying here counts as staying put.

        Opaque origins are never same-site — including with themselves. Two
        ``file://`` URLs are not a shared trust domain, and treating them as one
        would recreate the hole this module exists to close.
        """
        if self.is_opaque or other.is_opaque:
            return False
        return (self.scheme, self.host, self.port) == (other.scheme, other.host, other.port)

    def describe(self) -> str:
        """Human-facing label for approval prompts and audit lines."""
        if self.is_opaque:
            return f"{self.scheme}: (no site)" if self.scheme else "(unknown)"
        if self.port is None:
            return f"{self.scheme}://{self.host}"
        return f"{self.scheme}://{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.describe()


def parse_origin(url: str) -> Origin:
    """Extract the origin of ``url``. Anything without a host comes back opaque.

    Never raises. The gate calls this on whatever the model passed in, and a
    malformed URL must produce a denial, not an exception escaping the tool.
    """
    raw = url or ""
    # Read the scheme by hand: urlparse raises on a bad authority (an unclosed
    # IPv6 bracket, say) before we get to look at any field, so asking it first
    # would lose even the scheme we want for the audit line.
    scheme = raw.split(":", 1)[0].lower() if ":" in raw else ""
    if scheme in _OPAQUE_SCHEMES:
        # Keep the scheme so the audit trail says *which* opaque thing it was.
        return Origin(scheme=scheme, host="", port=None)
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        # Malformed authority (bad IPv6 literal, non-numeric port) — trust nothing.
        return Origin(scheme=scheme, host="", port=None)
    if not host:
        return Origin(scheme=scheme, host="", port=None)
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    return Origin(scheme=scheme, host=host, port=port)


def loggable_url(url: str) -> str:
    """A URL with its query values stripped, keeping the parameter names.

    A GET form submission carries what was typed in the query string, so writing
    the URL verbatim into the audit trail writes the password that trail exists
    to keep out. Names survive because "which fields were sent" is what makes an
    entry reconstructable; values do not.
    """
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return "<unparseable>"
    try:
        host, port = parsed.hostname or "", parsed.port
    except ValueError:  # a malformed port
        host, port = parsed.netloc, None
    # Rebuild the authority from host and port alone. Keeping ``netloc`` whole
    # kept ``user:password@`` with it — this function strips the query to keep a
    # typed password out of the trail and was writing one two fields to the left.
    netloc = f"{host}:{port}" if port is not None else host
    if parsed.username:
        netloc = f"<redacted-userinfo>@{netloc}"
    base = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    if not parsed.query:
        return base
    names = sorted({pair.split("=", 1)[0] for pair in parsed.query.split("&") if pair})
    return f"{base}?<redacted: {', '.join(names)}>"
