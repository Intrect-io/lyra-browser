import dataclasses
import os
from pathlib import Path

import pytest

from lyra_browser.config import Config, TrustedOrigins, parse_trusted_origins
from lyra_browser.origin import parse_origin


def test_data_dir_prefers_vega_data_dir(monkeypatch):
    monkeypatch.delenv("LYRA_BROWSER_DATA_DIR", raising=False)
    monkeypatch.setenv("VEGA_DATA_DIR", "/tmp/vega-x")
    cfg = Config()
    assert cfg.data_dir == Path("/tmp/vega-x/browser")
    assert cfg.profile_dir == Path("/tmp/vega-x/browser/profile")
    assert cfg.audit_path == Path("/tmp/vega-x/browser/audit.jsonl")


def test_explicit_browser_data_dir_wins(monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_DATA_DIR", "/tmp/explicit")
    monkeypatch.setenv("VEGA_DATA_DIR", "/tmp/vega-x")
    cfg = Config()
    assert cfg.data_dir == Path("/tmp/explicit")


def test_from_env_headless_and_approval(monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", "true")
    monkeypatch.setenv("LYRA_BROWSER_REQUIRE_APPROVAL", "off")
    cfg = Config.from_env()
    assert cfg.headless is True
    assert cfg.require_approval is False


def test_from_env_with_a_clean_environment_matches_the_declared_defaults(monkeypatch):
    """No field may mean one thing in the class and another when the server starts.

    ``from_env`` repeated every default as a literal, and one of them drifted:
    ``enforcement_mode`` read "observe" there while the field said "enforce", so
    the shipped server ran with its only enforcement layer off while the class,
    the README and every unit test agreed it was on. Verifications caught
    nothing because each script set the mode by hand.
    """
    for name in list(os.environ):
        if name.startswith("VEGA_"):
            monkeypatch.delenv(name, raising=False)

    declared, from_env = Config(), Config.from_env()

    drifted = {
        field.name: (getattr(declared, field.name), getattr(from_env, field.name))
        for field in dataclasses.fields(Config)
        if field.name not in {"data_dir", "profile_dir", "audit_path"}
        and getattr(declared, field.name) != getattr(from_env, field.name)
    }
    assert not drifted, f"from_env disagrees with the field default: {drifted}"


@pytest.mark.parametrize(
    ("field", "typo", "expected"),
    [
        ("enforcement_mode", "enfroce", "enforce"),
        ("consent_channel", "elict", "auto"),
        # A decision channel in the audit trail, not a way to configure asking.
        ("consent_channel", "trusted", "auto"),
    ],
)
def test_an_unreadable_choice_falls_back_to_the_declared_default(field, typo, expected):
    """A misspelt value must not become a third, weaker behaviour.

    ``enforcement_mode`` once landed in one that refused traffic like enforce
    while never spending a single-use grant. ``consent_channel`` fell through to
    ``legacy``, the one channel that takes the model's own word for an approval.
    """
    cfg = Config(**{field: typo})
    assert getattr(cfg, field) == expected


def test_capture_dir_defaults_under_data_dir(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LYRA_BROWSER_CAPTURE_DIR", raising=False)
    monkeypatch.delenv("VEGA_DATA_DIR", raising=False)
    monkeypatch.setenv("LYRA_BROWSER_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.capture_dir == tmp_path / "captures"


def test_capture_dir_lands_in_vega_uploads(monkeypatch, tmp_path) -> None:
    """VEGA attaches only files under its uploads root, so that is where we write."""
    monkeypatch.delenv("LYRA_BROWSER_CAPTURE_DIR", raising=False)
    monkeypatch.delenv("LYRA_BROWSER_DATA_DIR", raising=False)
    monkeypatch.setenv("VEGA_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.data_dir == tmp_path / "browser"
    assert cfg.capture_dir == tmp_path / "uploads" / "browser"


def test_capture_dir_explicit_override_wins(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VEGA_DATA_DIR", str(tmp_path / "vega"))
    monkeypatch.setenv("LYRA_BROWSER_CAPTURE_DIR", str(tmp_path / "elsewhere"))
    cfg = Config.from_env()
    assert cfg.capture_dir == tmp_path / "elsewhere"


def test_capture_keep_from_env_and_fallback(monkeypatch) -> None:
    monkeypatch.setenv("LYRA_BROWSER_CAPTURE_KEEP", "7")
    assert Config.from_env().capture_keep == 7
    monkeypatch.setenv("LYRA_BROWSER_CAPTURE_KEEP", "many")
    assert Config.from_env().capture_keep == 200


def test_ensure_dirs_creates_capture_dir(tmp_path) -> None:
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    cfg.capture_dir = tmp_path / "captures"
    cfg.ensure_dirs()
    assert cfg.capture_dir.is_dir()


# --------------------------------------------------------------------------
# Trusted origins — what an operator may pre-approve, and what never loads
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "entries"),
    [
        ("https://www.kvraudio.com", ("https://www.kvraudio.com",)),
        ("http://localhost:3000", ("http://localhost:3000",)),
        ("https://kvraudio.com:8443", ("https://kvraudio.com:8443",)),
        ("http://127.0.0.1:8080", ("http://127.0.0.1:8080",)),
        # A bare host is https on the default port: not http, not its subdomains.
        ("kvraudio.com", ("https://kvraudio.com",)),
        ("*.example.com", ("*.example.com",)),
        # Case folds, and the spellings of a default port agree.
        ("HTTPS://KVRAudio.COM:443/", ("https://kvraudio.com",)),
        ("http://Intranet.Test:80", ("http://intranet.test",)),
        ("*.Example.COM", ("*.example.com",)),
    ],
)
def test_trusted_origins_entry_forms(raw, entries):
    assert parse_trusted_origins(raw).entries == entries


def test_trusted_origins_split_on_commas_and_whitespace_and_collapse_duplicates():
    raw = "a.test,b.test  c.test\n\t*.d.test,, https://a.test:443 ,"

    assert parse_trusted_origins(raw).entries == (
        "https://a.test",
        "https://b.test",
        "https://c.test",
        "*.d.test",
    )


def test_a_sequence_of_entries_is_read_like_the_env_text():
    """A list handed over already split may still hold "a.test b.test" in one item,
    and non-strings are noise, not a crash."""
    raw = ("a.test b.test", "*.c.test,http://localhost:3000", None, 7, "junk!!")

    assert parse_trusted_origins(raw).entries == (
        "https://a.test",
        "https://b.test",
        "http://localhost:3000",
        "*.c.test",
    )
    assert parse_trusted_origins(None).entries == ()


# Each of these once looked like a site to somebody. None may load, and none may
# cost the entries around it.
UNVOUCHABLE = [
    # nothing, or nothing usable
    "",
    " ",
    ",",
    "*",
    "*.",
    ".",
    "https://",
    "http://:80",
    # a wildcard that trusts a class of sites rather than a site
    "*.com",
    "*.io",
    "**.example.com",
    "*.*.example.com",
    "foo.*.example.com",
    "*example.com",
    "*.example.*",
    # ... and the IP-shaped ones
    "*.127.0.0.1",
    "*.10.0.0.1",
    "*.1.1",
    "*.0x7f.1",
    "*.[::1]",
    # no authority, or not a document: nothing to trust
    "file:///etc/passwd",
    "data:text/plain",
    "about:blank",
    "blob:https://kvraudio.com/1",
    "javascript:alert(1)",
    "chrome://settings",
    "view-source:https://kvraudio.com",
    "ftp://kvraudio.com",
    "ws://kvraudio.com",
    # a site, not a URL: what a URL parser could read two ways is refused
    "https://kvraudio.com@evil.io",
    "https://user:pw@kvraudio.com",
    "https://kvraudio.com:443@evil.io",
    "https://kvraudio.com\\@evil.io",
    "https://kvraudio.com/forum",
    "https://kvraudio.com?x=1",
    "https://kvraudio.com#top",
    "kvraudio.com/forum",
    # a port takes the full form (http or https?), and has to be a port
    "kvraudio.com:8443",
    "localhost:3000",
    "https://kvraudio.com:0",
    "https://kvraudio.com:65536",
    "https://kvraudio.com:http",
    # a wildcard takes neither a scheme nor a port
    "https://*.example.com",
    "http://*.example.com",
    "*.example.com:8443",
    "*.example.com/x",
    # not a DNS name
    "-kvraudio.com",
    "kvraudio-.com",
    "kvraudio..com",
    ".kvraudio.com",
    "kvraudio.com.",
    "under_score.test",
    "kvraudio.com!",
    "a" * 64 + ".test",
    ("a" * 63 + ".") * 4 + "test",
    # non-ASCII, including a character that lowercases into plain ASCII
    "münchen.de",
    "\u212avraudio.com",
    "kvraudio.com\u3002evil.io",
    # IPv6 literals are not supported
    "https://[::1]:3000",
    "[::1]",
]


@pytest.mark.parametrize("raw", UNVOUCHABLE)
def test_trusted_origins_drop_what_they_cannot_vouch_for(raw):
    assert parse_trusted_origins(raw).entries == ()


def test_a_bad_trusted_entry_costs_nothing_but_itself():
    raw = "junk!! kvraudio.com file:///etc/passwd *.com *.example.com https://x.test/path"

    assert parse_trusted_origins(raw).entries == ("https://kvraudio.com", "*.example.com")


def test_trusted_entries_read_back_to_the_same_list():
    parsed = parse_trusted_origins("KVRaudio.com, http://localhost:3000/, *.Example.com a.test:443")

    assert parse_trusted_origins(parsed.entries) == parsed


@pytest.mark.parametrize(
    ("entry", "url", "matched"),
    [
        # a bare host: that host over https, and nothing near it
        ("kvraudio.com", "https://kvraudio.com/forum?q=1#top", "https://kvraudio.com"),
        ("kvraudio.com", "https://kvraudio.com:443/", "https://kvraudio.com"),
        ("kvraudio.com", "HTTPS://KVRAudio.COM/", "https://kvraudio.com"),
        ("kvraudio.com", "https://www.kvraudio.com/", None),
        ("kvraudio.com", "http://kvraudio.com/", None),
        ("kvraudio.com", "https://kvraudio.com:8443/", None),
        ("kvraudio.com", "https://kvraudio.com.evil.io/", None),
        ("kvraudio.com", "https://evilkvraudio.com/", None),
        ("kvraudio.com", "https://kvraudio.com@evil.io/", None),
        ("kvraudio.com", "https://kvraudio.com:443@evil.io/", None),
        ("kvraudio.com", "https://kvraudio.com%2eevil.io/", None),
        ("kvraudio.com", "https://kvraudio.com./", None),
        # a full origin: exactly that scheme, host and port
        ("https://www.kvraudio.com", "https://www.kvraudio.com/x", "https://www.kvraudio.com"),
        ("https://www.kvraudio.com", "https://kvraudio.com/", None),
        ("http://localhost:3000", "http://localhost:3000/app", "http://localhost:3000"),
        ("http://localhost:3000", "http://localhost:3001/", None),
        ("http://localhost:3000", "https://localhost:3000/", None),
        ("http://localhost:3000", "http://localhost/", None),
        # a wildcard: any subdomain over https on the default port, never the apex
        ("*.example.com", "https://a.example.com/", "*.example.com"),
        ("*.example.com", "https://a.b.example.com/", "*.example.com"),
        ("*.example.com", "https://example.com/", None),
        ("*.example.com", "http://a.example.com/", None),
        ("*.example.com", "https://a.example.com:8443/", None),
        ("*.example.com", "https://evilexample.com/", None),
        ("*.example.com", "https://a.example.com.evil.io/", None),
        ("*.example.com", "https://example.com.evil.io/", None),
        ("*.example.com", "https://example.com@evil.io/", None),
        ("*.example.com", "https://.example.com/", None),
        ("*.example.com", "https://a..example.com/", None),
        # a document with no site is nobody's to vouch for
        ("kvraudio.com", "file:///etc/passwd", None),
        ("kvraudio.com", "data:text/html,hi", None),
        ("*.example.com", "about:blank", None),
        ("*.example.com", "", None),
    ],
)
def test_a_trusted_entry_vouches_for_exactly_what_it_names(entry, url, matched):
    assert parse_trusted_origins(entry).entry_for(parse_origin(url)) == matched


def test_a_list_built_by_hand_still_refuses_an_opaque_origin():
    local = parse_origin("file:///etc/passwd")

    assert TrustedOrigins(origins=(local,)).entry_for(local) is None


def test_config_keeps_only_the_trusted_origins_it_can_read():
    cfg = Config(
        trusted_origins=("KVRaudio.com", "junk!!", "file:///etc/passwd", "kvraudio.com", "*.com")
    )

    assert cfg.trusted_origins == ("https://kvraudio.com",)


def test_from_env_reads_the_trusted_origins(monkeypatch):
    monkeypatch.setenv(
        "LYRA_BROWSER_TRUSTED_ORIGINS",
        "kvraudio.com, *.example.com\nhttp://localhost:3000 junk!!",
    )

    cfg = Config.from_env()

    assert sorted(cfg.trusted_origins) == [
        "*.example.com",
        "http://localhost:3000",
        "https://kvraudio.com",
    ]


@pytest.mark.parametrize(
    "value", [None, "", "  ", ",,", "*", "file:///etc/passwd, data:text/plain"]
)
def test_from_env_trusts_nothing_it_was_not_told_to(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LYRA_BROWSER_TRUSTED_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("LYRA_BROWSER_TRUSTED_ORIGINS", value)

    assert Config.from_env().trusted_origins == ()


def test_submit_trusted_origins_come_from_their_own_variable(monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_TRUSTED_ORIGINS", "roam.example")
    monkeypatch.setenv("LYRA_BROWSER_TRUSTED_SEND_ORIGINS", "KVRaudio.com junk!! *.Example.com")
    cfg = Config.from_env()
    assert cfg.trusted_origins == ("https://roam.example",)
    assert cfg.trusted_send_origins == ("https://kvraudio.com", "*.example.com")


def test_the_submit_list_is_empty_unless_the_operator_sets_it(monkeypatch):
    monkeypatch.delenv("LYRA_BROWSER_TRUSTED_SEND_ORIGINS", raising=False)
    assert Config.from_env().trusted_send_origins == ()


@pytest.mark.parametrize(
    ("raw", "size"),
    [
        ("390x844", (390, 844)),
        (" 768X1024 ", (768, 1024)),
        (None, (1280, 800)),
        ("", (1280, 800)),
        ("390", (1280, 800)),
        ("390x", (1280, 800)),
        ("99x844", (1280, 800)),  # too small to be a layout
        ("390x99999", (1280, 800)),
        ("390x844; rm", (1280, 800)),
    ],
)
def test_viewport_env_is_a_size_or_the_default(monkeypatch, raw, size):
    if raw is None:
        monkeypatch.delenv("LYRA_BROWSER_VIEWPORT", raising=False)
    else:
        monkeypatch.setenv("LYRA_BROWSER_VIEWPORT", raw)
    cfg = Config.from_env()
    assert (cfg.viewport_width, cfg.viewport_height) == size


def test_old_vega_browser_env_names_still_work(monkeypatch) -> None:
    monkeypatch.delenv("LYRA_BROWSER_PROXY", raising=False)
    monkeypatch.setenv("VEGA_BROWSER_PROXY", "http://old:1")
    assert Config.from_env().proxy == "http://old:1"
    monkeypatch.setenv("LYRA_BROWSER_PROXY", "http://new:2")
    assert Config.from_env().proxy == "http://new:2"


def test_home_data_dir_old_fallback_only_when_alone(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LYRA_BROWSER_DATA_DIR", raising=False)
    monkeypatch.delenv("VEGA_BROWSER_DATA_DIR", raising=False)
    monkeypatch.delenv("VEGA_DATA_DIR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert Config.from_env(client="generic").data_dir == tmp_path / ".lyra-browser"
    (tmp_path / ".vega-browser").mkdir()
    assert Config.from_env(client="generic").data_dir == tmp_path / ".vega-browser"
    (tmp_path / ".lyra-browser").mkdir()
    assert Config.from_env(client="generic").data_dir == tmp_path / ".lyra-browser"
