"""Configuration for the lyra-browser MCP server.

Paths default into the data dir of whichever agent harness spawned the server
(VEGA or Hermes) so the browser profile, audit trail and captures sit where
that client can use them. Every value can be overridden by env vars, which is
how a client passes per-install settings through its MCP server config.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .origin import Origin, parse_origin

# Which agent harness is on the other end of the wire. It decides defaults
# only — where data and captures go, whether a window is expected, and how
# captures reach the model — never how permissions are judged.
CLIENTS = frozenset({"vega", "hermes", "generic", "remote"})


def _env(name: str, default: str | None = None) -> str | None:
    """Read ``LYRA_BROWSER_*``, falling back to the pre-rename ``VEGA_BROWSER_*``.

    The browser used to be called vega-browser; installs that still export the
    old names keep working. The new name wins when both are set.
    """
    value = os.environ.get(name)
    if value is None and name.startswith("LYRA_BROWSER_"):
        value = os.environ.get("VEGA_BROWSER_" + name.removeprefix("LYRA_BROWSER_"))
    return default if value is None else value


def _home_data_dir() -> Path:
    """``~/.lyra-browser``, or the old ``~/.vega-browser`` while only that exists."""
    new = Path.home() / ".lyra-browser"
    old = Path.home() / ".vega-browser"
    return old if old.exists() and not new.exists() else new


def _detect_client() -> str:
    """Name the client from the environment it gave us.

    Order: LYRA_BROWSER_CLIENT > VEGA_DATA_DIR (vega) > HERMES_HOME (hermes) >
    generic. Hermes passes its stdio servers a whitelisted environment without
    HERMES_HOME, so a Hermes install usually says so explicitly (``--client
    hermes``); detection is the convenience, not the contract.
    """
    explicit = (_env("LYRA_BROWSER_CLIENT") or "").strip().lower()
    if explicit in CLIENTS:
        return explicit
    if os.environ.get("VEGA_DATA_DIR"):
        return "vega"
    if os.environ.get("HERMES_HOME"):
        return "hermes"
    return "generic"


def _hermes_home() -> Path:
    """Hermes' home dir, resolved the way Hermes resolves it."""
    explicit = os.environ.get("HERMES_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
    return Path.home() / ".hermes"


def _default_data_dir(client: str = "generic") -> Path:
    """Resolve the base data dir for ``client``.

    Order: LYRA_BROWSER_DATA_DIR > VEGA_DATA_DIR/browser (vega) >
    HERMES_HOME/browser (hermes) > ~/.lyra-browser.

    One dir per client is deliberate: the profile holds real logins and
    Chromium locks it, so two harnesses sharing one would have the second
    launch fail while the first is open.
    """
    explicit = _env("LYRA_BROWSER_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser()
    vega = os.environ.get("VEGA_DATA_DIR")
    if client == "vega" and vega:
        return Path(vega).expanduser() / "browser"
    if client == "hermes":
        return _hermes_home() / "browser"
    return _home_data_dir()


def _default_capture_dir(data_dir: Path, client: str = "generic") -> Path:
    """Where screenshots and element captures are written.

    Order: LYRA_BROWSER_CAPTURE_DIR > VEGA_DATA_DIR/uploads/browser (vega) >
    HERMES_HOME/cache/browser (hermes) > <data_dir>/captures.

    The middle two are deliberate coupling, one per client. VEGA only attaches
    an image to a model turn when the file sits under its own uploads root
    (``pipeline/image_history.py``). Hermes lets the model look at a local file
    through ``vision_analyze``, and under a sandboxed terminal backend reads
    host paths only inside ``$HERMES_HOME/cache`` and its siblings
    (``tools/image_source.py``). A capture written anywhere else is a file the
    model never sees.
    """
    explicit = _env("LYRA_BROWSER_CAPTURE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    vega = os.environ.get("VEGA_DATA_DIR")
    if client == "vega" and vega:
        return Path(vega).expanduser() / "uploads" / "browser"
    if client == "hermes":
        return _hermes_home() / "cache" / "browser"
    return data_dir / "captures"


def _default_download_dir(data_dir: Path) -> Path:
    """Where files the agent asked to download are saved.

    Order: LYRA_BROWSER_DOWNLOAD_DIR > <data_dir>/downloads. Unlike captures, no
    client has a place its model reads from by default, so this follows the data
    dir — which is already per client.
    """
    explicit = _env("LYRA_BROWSER_DOWNLOAD_DIR")
    if explicit:
        return Path(explicit).expanduser()
    return data_dir / "downloads"


def _has_display() -> bool:
    """Whether a headful window could open here.

    Only Linux can tell from the environment: a desktop session exports
    DISPLAY or WAYLAND_DISPLAY, and a client that strips them (Hermes keeps a
    whitelist) or a server without one leaves neither. macOS and Windows open
    windows without either variable.
    """
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _default_headless(client: str) -> bool:
    """Headless unless someone could be at the window.

    Hermes answers through chat — CLI, Telegram, Slack — not at the browser,
    and does not hand its servers a display, so its default is headless. A
    remote client reaches a server nobody sits in front of, so it is headless
    too. A Linux process with no display cannot open a window at all;
    launching headful there is a crash, headless is a working session that
    reports ``attended=false``.
    """
    return client in ("hermes", "remote") or not _has_display()


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _as_viewport(value: str | None, default: tuple[int, int]) -> tuple[int, int]:
    """``WIDTHxHEIGHT`` (``390x844``); anything else, or a side outside 200..4000, is the default.

    A viewport is how a layout is judged at phone and tablet width, so a typo must not
    silently launch at some other size: the default is the size the tests expect.
    """
    match = re.fullmatch(r"\s*(\d{3,4})\s*[xX]\s*(\d{3,4})\s*", value or "")
    if not match:
        return default
    width, height = int(match.group(1)), int(match.group(2))
    if not (200 <= width <= 4000 and 200 <= height <= 4000):
        return default
    return width, height


def _as_float(value: str | None, default: float) -> float:
    """Parse a numeric env var, falling back rather than crashing the server."""
    try:
        return float(value) if value else default
    except ValueError:
        return default


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_optional_bool(value: str | None) -> bool | None:
    """Like ``_as_bool``, but unset (or blank) stays None: "not decided"."""
    if value is None or not value.strip():
        return None
    return _as_bool(value, default=False)


# Fields whose value has to be one of a fixed set. An unreadable value is not a
# third behaviour: `enforcement_mode` once fell into one that refused traffic but
# never spent a grant, and an unreadable `consent_channel` lands on the weakest
# one there is. Anything unrecognised goes back to the declared default.
_CHOICES: dict[str, frozenset[str]] = {
    "enforcement_mode": frozenset({"observe", "enforce"}),
    "consent_channel": frozenset({"auto", "elicit", "legacy", "off"}),
    "driver": frozenset({"auto", "patchright", "playwright"}),
    "guard_backend": frozenset({"route", "cdp"}),
}


# --------------------------------------------------------------------------
# Trusted origins — sites the operator has pre-approved
# --------------------------------------------------------------------------

# One DNS label: ASCII letters, digits and inner hyphens. ASCII only, on purpose:
# a lookalike in another script must not be spellable here (an IDN is written in
# the punycode form a browser reports anyway).
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# ``[scheme://]host[:port][/]`` and nothing else. Userinfo, path, query and
# fragment are refused rather than interpreted: an entry that a URL parser could
# read two ways (``https://kvraudio.com@evil.io``) is not a site anyone meant.
_ENTRY = re.compile(r"(?:(?P<scheme>https?)://)?(?P<host>[a-z0-9.-]+)(?::(?P<port>[0-9]{1,5}))?/?")
_LIST_SEPARATOR = re.compile(r"[,\s]+")


def _is_hostname(host: str) -> bool:
    """A dotted ASCII name (an IPv4 literal passes): every label well formed, none empty."""
    return len(host) <= 253 and all(_LABEL.fullmatch(label) for label in host.split("."))


def _exact_origin(entry: str) -> Origin | None:
    """``https://host[:port]`` as written; a bare ``host`` is ``https://host``."""
    found = _ENTRY.fullmatch(entry)
    if found is None or not _is_hostname(found["host"]):
        return None
    scheme, port = found["scheme"], found["port"]
    if scheme is None:
        if port is not None:
            # "localhost:3000" leaves http-or-https open; a port takes the full form.
            return None
        scheme = "https"
    number = int(port) if port is not None else None
    if number is not None and not 0 < number < 65536:
        return None
    # Through parse_origin, so an entry and a page URL are normalised by the same
    # code (a default port folds away) and can only differ in what they say.
    origin = parse_origin(f"{scheme}://{found['host']}" + (f":{number}" if number else ""))
    return None if origin.is_opaque else origin


def _wildcard_suffix(entry: str) -> str | None:
    """``*.example.com`` -> ``example.com``, or None when it would trust too much.

    Two labels at least (``*.com`` is every .com site), and a last label that
    starts with a letter: a TLD is never numeric, which is what refuses
    ``*.127.0.0.1`` and every other IP-shaped base. Whether a two-label base is
    itself a public suffix (``*.co.uk``, ``*.github.io``) is a list this module
    does not carry; the operator has to know that.
    """
    if not entry.startswith("*."):
        return None
    base = entry[2:]
    if not _is_hostname(base) or "." not in base or not base.rpartition(".")[2][0].isalpha():
        return None
    return base


@dataclass(frozen=True, slots=True)
class TrustedOrigins:
    """Sites the operator has pre-approved. Build with ``parse_trusted_origins``."""

    origins: tuple[Origin, ...] = ()
    """Whole origins: a full-origin entry, or a bare host read as ``https://host``."""
    suffixes: tuple[str, ...] = ()
    """``example.com`` for ``*.example.com``: https on the default port, at any depth
    below it, and never ``example.com`` itself."""

    @property
    def entries(self) -> tuple[str, ...]:
        """The list in canonical text form. Parsing it back gives an equal list."""
        exact = (origin.describe() for origin in self.origins)
        return (*exact, *(f"*.{suffix}" for suffix in self.suffixes))

    def entry_for(self, origin: Origin) -> str | None:
        """The entry that vouches for ``origin``, or None.

        Compares ``(scheme, host, port)`` fields and never a string prefix:
        prefixes are how ``kvraudio.com.evil.io`` and ``kvraudio.com@evil.io``
        get through.
        """
        if origin.is_opaque:
            return None
        if origin in self.origins:
            return origin.describe()
        if origin.scheme == "https" and origin.port is None:
            for suffix in self.suffixes:
                head = origin.host.removesuffix("." + suffix)
                # Something real must stand in front of the dot: `*.example.com`
                # is not `example.com`, nor `.example.com`, nor `a..example.com`.
                if head != origin.host and _is_hostname(head):
                    return f"*.{suffix}"
        return None


def parse_trusted_origins(raw: str | Iterable[str] | None) -> TrustedOrigins:
    """Read the operator's list of trusted sites; whatever it cannot vouch for is dropped.

    ``raw`` is the env text, or a sequence of such texts; either way entries are
    separated by commas and/or whitespace. Three forms are understood, and nothing else:

    * ``https://www.kvraudio.com``, ``http://localhost:3000`` — exactly that
      scheme, host and port (a default port is the same as none).
    * ``kvraudio.com`` — ``https`` only, that host only: not its subdomains, not http.
    * ``*.example.com`` — ``https`` only, any subdomain of ``example.com`` on the
      default port, but not ``example.com`` itself.

    Everything else is dropped without a word, because a list that refuses to
    load locks the operator out over a typo while a list that guesses opens a
    hole: empty and garbage entries, non-ASCII text, userinfo, paths, queries, a
    port on a bare host, IPv6 literals, any scheme but http(s) (``file:``,
    ``data:``, ``about:``, ``blob:`` name no site), a lone ``*``, single-label
    wildcards (``*.com``) and wildcards on an IP literal. Hosts are lowercased.
    """
    if raw is None:
        texts: Iterable[str] = ()
    elif isinstance(raw, str):
        texts = (raw,)
    else:
        texts = (item for item in raw if isinstance(item, str))
    origins: dict[Origin, None] = {}  # insertion-ordered and de-duplicated
    suffixes: dict[str, None] = {}
    for token in (token for text in texts for token in _LIST_SEPARATOR.split(text)):
        if not token.isascii():
            # Checked before lower(): the Kelvin sign lowercases to a plain "k".
            continue
        entry = token.lower()
        if (suffix := _wildcard_suffix(entry)) is not None:
            suffixes[suffix] = None
        elif (origin := _exact_origin(entry)) is not None:
            origins[origin] = None
    return TrustedOrigins(tuple(origins), tuple(suffixes))


@dataclass(slots=True)
class Config:
    """Runtime configuration, populated from env with sane local-first defaults."""

    # The agent harness serving as MCP client — see CLIENTS. Resolved first
    # because the paths and the headless default below derive from it.
    client: str = field(default_factory=_detect_client)
    # None until __post_init__ resolves it for ``client``.
    data_dir: Path | None = None
    # Persistent Chromium profile dir — keeps logins/cookies between runs.
    profile_dir: Path = field(init=False)
    audit_path: Path = field(init=False)
    # Where captures land — derived like profile_dir, see _default_capture_dir.
    capture_dir: Path = field(init=False)
    # How many capture files to keep. Every screenshot is a new file, so an
    # agent that looks at the page on every turn would otherwise fill the disk
    # over a long session. Oldest go first; 0 disables pruning.
    capture_keep: int = 200

    # Where downloads land — derived like capture_dir, see _default_download_dir.
    download_dir: Path = field(init=False)
    # How many saved downloads to keep there. Only files this server saved are
    # ever deleted, however this dir was chosen: an operator may point it at a
    # folder that holds other things. Oldest go first; 0 keeps every one.
    download_keep: int = 200
    # The largest file that is kept, in bytes. A bigger one is deleted and
    # reported rather than saved. 0 lifts the limit.
    download_max_bytes: int = 200 * 1024 * 1024
    # How long a call that asked for a download waits for the file once it has
    # started, before it is cancelled. A download that never starts is a shorter
    # wait — the call's own timeout.
    download_timeout_s: float = 120.0
    # How long a call that did NOT ask for a download keeps listening after it
    # acts, so one the page starts is reported as blocked instead of passing for a
    # plain success. Measured: the browser's download event lands within ~20ms of
    # the click returning, hence 100ms. The same window is how long a click, a key press
    # and a typed Enter go on listening for a navigation the guard refuses or a tab the
    # page opens (tools/aftermath.py); hover and plain typing linger at most 40ms of it.
    # 0 turns listening after an action off.
    download_settle_s: float = 0.1

    # Browser window. None means "decide for this client and host" (see
    # _default_headless): headful where someone can watch, headless where
    # nobody can. An explicit True/False always wins.
    headless: bool | None = None
    # Mirror every audit record to stderr as well as audit.jsonl. A container's
    # disk is gone when it sleeps; its stderr is collected by the platform.
    audit_stderr: bool = False
    # Explicit channel override (e.g. "chrome", "msedge"). When unset, the session
    # tries the user's installed browsers in order — see browser_candidates().
    channel: str | None = None
    # Allow falling back to Playwright's bundled Chromium. That binary only exists
    # after `playwright install chromium`, which an end user with only VEGA.app has
    # NOT run — so for them this fallback is a no-op and they rely on system Chrome.
    allow_bundled_fallback: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    # Route the browser through this proxy (e.g. "socks5://host:1080"), for UAT runs
    # that must not share the operator's egress IP: per-IP rate limits, quotas and
    # IP-based analytics exclusion all key on it. None means a direct connection.
    proxy: str | None = None

    # Human-in-the-loop. When True, risk-gated actions require an explicit
    # confirm=True before they execute.
    require_approval: bool = True

    # How consent is obtained:
    #   "elicit" — ask over MCP. The answer arrives from the client, so the model
    #              cannot forge it. Requires a client that implements elicitation.
    #   "legacy" — honour confirm=True. That is the model's own assertion, not a
    #              person's, and it is recorded as such. Default because VEGA does
    #              not implement elicitation yet and the alternative is denying
    #              every gated action.
    #   "off"    — do not ask.
    consent_channel: str = "auto"
    # How long an unanswered prompt holds its tool call before it becomes a
    # denial. Five minutes suits a person at a screen; a gateway nobody is
    # watching wants it short (docs/HERMES_INTEGRATION.md).
    consent_timeout_s: float = 300.0
    # Sites the operator has pre-approved for NAVIGATE (and the INTERACT it
    # implies), so one they always use is not asked about again. Entry forms:
    # see parse_trusted_origins. An answer, not a consent channel, and never a
    # single-use scope. Normalised in __post_init__: unreadable entries are gone.
    trusted_origins: tuple[str, ...] = ()
    # Sites where the operator has also pre-approved *sending*: SUBMIT (a non-idempotent
    # request sent from that origin) and UPLOAD (a local file handed to its page), for
    # acceptance runs against one's own product. A separate list on purpose:
    # ``trusted_origins`` never answers a single-use scope, and a site listed there must
    # not become send-trusted by being roaming-trusted. Same entry forms. Never PUBLISH or
    # DOWNLOAD, and each approval stays single-use.
    trusted_send_origins: tuple[str, ...] = ()
    # How long a grant stays usable. Effects that cannot be undone ignore this
    # and are single-use regardless (see permission.py).
    grant_ttl_s: float = 600.0
    # How long a finished action's unused one-shot scope survives. Not zero: a
    # click's navigation can leave just after the call returns, and reclaiming
    # before then refuses the submission the user approved.
    scope_release_grace_s: float = 2.0
    # How long a holder may go quiet before another session may claim the
    # browser. Closing is owner-only, so without a timeout a client that drops
    # with its window open locks the server for the life of the process. The
    # handoff closes the window, so this is long enough that a person watching
    # one the agent is not currently driving does not lose it.
    owner_idle_timeout_s: float = 900.0
    # "enforce" blocks navigations that no grant covers; "observe" only records
    # what it would have blocked, which is how a change to this layer gets
    # measured for false positives before it is switched on.
    # Downloads follow the same switch (downloads.py): enforce cancels one that no
    # grant covers, observe saves it anyway and records it as `would_block`.
    #
    # Note that observe is deliberately more permissive than enforce in one way:
    # it does not spend single-use grants, so an approved submission stays usable
    # and a later undeclared one can slip past the observation. It measures false
    # positives accurately; it under-reports what enforce would catch.
    enforcement_mode: str = "enforce"
    # Which Playwright to launch with. "patchright" is a drop-in fork that does
    # not send Runtime.enable — the one CDP call Kasada refuses on
    # — and "auto" takes it when installed. See session.load_driver.
    driver: str = "auto"
    # Where the navigation guard sits. "route" is Playwright's context.route: it sees
    # the first request of a redirect chain only, and Playwright's cache-disable that
    # comes with it is what DataDome (etsy.com) refuses. "cdp" judges documents only,
    # redirect hops included, from a second DevTools connection over a loopback
    # debugging port (cdp_guard.py); that port is an open service, see README.md
    # "Debugging port". With "cdp" the session closes the window if the guard is lost.
    guard_backend: str = "route"

    def __post_init__(self) -> None:
        if self.client not in CLIENTS:
            self.client = _detect_client()
        if self.data_dir is None:
            self.data_dir = _default_data_dir(self.client)
        if self.headless is None:
            self.headless = _default_headless(self.client)
        self.profile_dir = self.data_dir / "profile"
        self.audit_path = self.data_dir / "audit.jsonl"
        self.capture_dir = _default_capture_dir(self.data_dir, self.client)
        self.download_dir = _default_download_dir(self.data_dir)
        for name, allowed in _CHOICES.items():
            if getattr(self, name) not in allowed:
                setattr(self, name, type(self).__dataclass_fields__[name].default)
        # Turning approval off is an explicit instruction not to ask anyone.
        if not self.require_approval:
            self.consent_channel = "off"
        # Keep only what could be read as a site; the text left is canonical.
        self.trusted_origins = parse_trusted_origins(self.trusted_origins).entries
        self.trusted_send_origins = parse_trusted_origins(self.trusted_send_origins).entries

    @property
    def attended(self) -> bool:
        """Whether a human can actually see (and grab) the window we drive.

        Headless is not merely "no window" — it means nobody is watching, so the
        collaboration primitives (highlight, ask-the-user, takeover) have no one
        to talk to and must say so instead of pretending they worked.
        """
        return not self.headless

    @classmethod
    def from_env(cls, client: str | None = None) -> Config:
        """Build from the environment. ``client`` (the ``--client`` flag) beats
        detection; paths and the headless default follow whichever wins."""
        return cls(
            client=client if client in CLIENTS else _detect_client(),
            headless=_as_optional_bool(_env("LYRA_BROWSER_HEADLESS")),
            viewport_width=_as_viewport(
                _env("LYRA_BROWSER_VIEWPORT"),
                (
                    cls.__dataclass_fields__["viewport_width"].default,
                    cls.__dataclass_fields__["viewport_height"].default,
                ),
            )[0],
            viewport_height=_as_viewport(
                _env("LYRA_BROWSER_VIEWPORT"),
                (
                    cls.__dataclass_fields__["viewport_width"].default,
                    cls.__dataclass_fields__["viewport_height"].default,
                ),
            )[1],
            channel=_env("LYRA_BROWSER_CHANNEL") or None,
            proxy=_env("LYRA_BROWSER_PROXY") or None,
            allow_bundled_fallback=_as_bool(_env("LYRA_BROWSER_ALLOW_BUNDLED"), default=True),
            audit_stderr=_as_bool(_env("LYRA_BROWSER_AUDIT_STDERR"), default=False),
            require_approval=_as_bool(_env("LYRA_BROWSER_REQUIRE_APPROVAL"), default=True),
            consent_channel=(
                _env("LYRA_BROWSER_CONSENT_CHANNEL")
                or cls.__dataclass_fields__["consent_channel"].default
            ),
            consent_timeout_s=_as_float(_env("LYRA_BROWSER_CONSENT_TIMEOUT"), default=300.0),
            trusted_origins=parse_trusted_origins(_env("LYRA_BROWSER_TRUSTED_ORIGINS")).entries,
            trusted_send_origins=parse_trusted_origins(
                _env("LYRA_BROWSER_TRUSTED_SEND_ORIGINS")
            ).entries,
            grant_ttl_s=_as_float(_env("LYRA_BROWSER_GRANT_TTL"), default=600.0),
            scope_release_grace_s=_as_float(
                _env("LYRA_BROWSER_RELEASE_GRACE"),
                default=cls.__dataclass_fields__["scope_release_grace_s"].default,
            ),
            owner_idle_timeout_s=_as_float(
                _env("LYRA_BROWSER_OWNER_IDLE_TIMEOUT"),
                default=cls.__dataclass_fields__["owner_idle_timeout_s"].default,
            ),
            # Read the field default rather than repeating it: this line said
            # "observe" while the field said "enforce", so the server everyone runs
            # had its only enforcement layer switched off while the docs, the tests
            # and the class all claimed otherwise.
            enforcement_mode=(
                _env("LYRA_BROWSER_ENFORCEMENT")
                or cls.__dataclass_fields__["enforcement_mode"].default
            ),
            driver=(_env("LYRA_BROWSER_DRIVER") or cls.__dataclass_fields__["driver"].default),
            guard_backend=(
                _env("LYRA_BROWSER_GUARD") or cls.__dataclass_fields__["guard_backend"].default
            ),
            capture_keep=_as_int(
                _env("LYRA_BROWSER_CAPTURE_KEEP"),
                default=cls.__dataclass_fields__["capture_keep"].default,
            ),
            download_keep=_as_int(
                _env("LYRA_BROWSER_DOWNLOAD_KEEP"),
                default=cls.__dataclass_fields__["download_keep"].default,
            ),
            download_max_bytes=_as_int(
                _env("LYRA_BROWSER_DOWNLOAD_MAX_BYTES"),
                default=cls.__dataclass_fields__["download_max_bytes"].default,
            ),
            download_timeout_s=_as_float(
                _env("LYRA_BROWSER_DOWNLOAD_TIMEOUT"),
                default=cls.__dataclass_fields__["download_timeout_s"].default,
            ),
        )

    @property
    def instances_dir(self) -> Path:
        """Where a server that finds the profile taken keeps its private one.

        A sibling of ``data_dir`` (``<data_dir>-instances/<pid>/profile``), not a
        child: the profile and everything beside it stay one tree to back up or
        wipe. The Hermes wrapper uses the same name when it steps aside first.
        """
        return self.data_dir.with_name(self.data_dir.name + "-instances")

    def browser_candidates(self) -> list[str | None]:
        """Ordered launch targets. ``None`` means Playwright's bundled Chromium.

        Default order reuses an already-installed browser (no ~150MB download),
        which is what an end user with only VEGA.app needs. An explicit channel
        override is tried first; the bundled fallback is appended only when allowed.
        """
        candidates: list[str | None] = [self.channel] if self.channel else ["chrome", "msedge"]
        if self.allow_bundled_fallback:
            candidates.append(None)
        return candidates

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.capture_dir.mkdir(parents=True, exist_ok=True)
