"""Playwright headful session manager.

Owns a single visible Chromium window backed by a persistent profile, so the
user watches (and can grab) the exact session the agent drives. The agent works
in one tab at a time — the *active* page. It follows onto tabs a page opens
(``_adopt_page``) and, when the active tab goes away, falls back to where it
came from (``page``), so a popup that closes itself never strands the session on
a dead page. ``tools/tabs.py`` is how the agent chooses a tab on purpose.
Only a claiming call may put a window back on the screen. Reads ask for
``page(revive=False)``, which can fall back to a tab that exists but raises
``BrowserClosed`` rather than open a tab or relaunch a browser that was closed.

Browser resolution is deliberately end-user friendly: VEGA's target user has only
VEGA.app — no pip, no ``playwright install``. So we reuse an already-installed
browser (Chrome/Edge) by default and never assume the bundled Chromium exists.
When no browser is usable we raise BrowserUnavailable, which carries a structured
envelope VEGA's UI can turn into a "install Chrome" prompt instead of crashing.
"""

from __future__ import annotations

import asyncio
import errno
import importlib
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cdp_guard import EXIT_GRACE_S, CdpGuard, CdpGuardError, prepare_profile
from .config import Config
from .dialogs import DialogHandler

try:  # POSIX only. Without it the profile is simply not guarded, as it never was.
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:  # avoid importing Playwright at module import time
    from playwright.async_api import BrowserContext, Page, Playwright

# The one token by which a headless Chrome's UA differs from a windowed one.
_HEADLESS_MARK = "HeadlessChrome"
# What ``window.screen`` reports in headless, where there is no real screen. A
# desktop size, so the window we emulate fits on it — an emulated viewport
# otherwise reports the viewport as the screen, and a window larger than the
# screen it is on is a geometry no person's browser has.
_HEADLESS_SCREEN = (1920, 1080)
# Where the per-channel UA override is remembered, under the data dir.
_UA_CACHE_NAME = "browser-ua.json"
# The profile claim, a sibling of the profile directory (see ProfileLock).
_PROFILE_LOCK_NAME = "profile.lock"
# Playwright default switches the cdp guard backend launches without. Playwright adds
# --enable-unsafe-swiftshader unconditionally; on a host with no GPU it is the only
# default that changes anything a page can read (a SwiftShader WebGL where plain
# Chrome has none), and DataDome refuses a browser that has it.
_CDP_DROPPED_DEFAULTS = ("--enable-unsafe-swiftshader",)


# Import order per driver preference. Both packages expose the same API.
_DRIVER_ORDER = {
    "auto": ("patchright", "playwright"),
    "patchright": ("patchright",),
    "playwright": ("playwright",),
}


def load_driver(preference: str = "auto") -> tuple[str, object]:
    """The Playwright to launch with: ``(name, async_playwright)``.

     patchright is a drop-in fork of Playwright that does not send
     ``Runtime.enable`` — the one CDP call Kasada refuses on, measured in
    (raw CDP without it passes; with it alone, 429) — and evaluates in
     an isolated world instead. Everything this module does (persistent context,
     ``context.route``, ``new_cdp_session``) is the same API, so the choice is
     made here once. ``auto`` prefers patchright when it is installed and falls
     back to playwright, which is what a VEGA runtime that bundles only
     playwright gets; a deployment pins one with ``LYRA_BROWSER_DRIVER``.
    """
    order = _DRIVER_ORDER.get(preference) or _DRIVER_ORDER["auto"]
    missing: list[str] = []
    for name in order:
        try:
            module = importlib.import_module(f"{name}.async_api")
        except ImportError:
            missing.append(name)
            continue
        return name, module.async_playwright
    raise ImportError(f"no browser driver installed (tried {', '.join(missing)})")


def _fixed_user_agent(real: str) -> str | None:
    """The UA this Chrome would carry with a window, or None if it already does."""
    if _HEADLESS_MARK not in real:
        return None
    return real.replace(_HEADLESS_MARK, "Chrome")


class UserAgentCache:
    """The headless UA override for each channel, remembered across launches.

    The override has to carry the real Chrome version, and the only reliable
    way to read that is to launch the browser. So the first headless launch
    reads it, relaunches with the override, and remembers it here; every later
    launch starts with the override and only confirms it against the running
    binary. A Chrome update makes the entry stale and costs one relaunch.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def get(self, channel: str) -> str | None:
        return self._load().get(channel)

    def put(self, channel: str, user_agent: str | None) -> None:
        """Remember ``user_agent`` for ``channel``; None forgets it.

        Written through a temp file so a reader never sees a partial JSON and
        a crash mid-write leaves the previous entry, not an empty file.
        """
        data = self._load()
        if user_agent is None:
            data.pop(channel, None)
        else:
            data[channel] = user_agent
        tmp = self._path.with_suffix(".json.part")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            # A cache that cannot be written costs a relaunch next time; it
            # must not cost the session.
            pass


def launch_kwargs(
    config: Config,
    channel: str | None,
    user_agent: str | None = None,
    profile_dir: Path | None = None,
) -> dict:
    """Playwright launch options for one browser candidate.

    They make the session look, to a page, like the same Chrome opened by hand,
    because that is what sites which sort agents from people check
    (of 34 login pages plain Chrome passes, the defaults failed 6 headful and 19
    headless; these settings bring that to 0 and 2):

    - ``--disable-blink-features=AutomationControlled``. Playwright's defaults
      leave ``navigator.webdriver`` true, and Cloudflare's managed challenge
      refuses on that alone (kvraudio, gitlab, glassdoor). Playwright's own MCP
      server ships the same switch.
    - Headful: no emulated viewport. Emulation reports ``screen`` as the
      viewport, so the window is larger than the screen it is on. The window
      is sized instead and the page sees the real screen.
    - Headless: keep the viewport (there is no window to size) and give
      ``screen`` a desktop size so the geometry is possible.
    - Headless: ``user_agent`` without the ``HeadlessChrome`` token, which is the
      one thing that marks the UA of a headless Chrome and gets it a challenge
      on its own (13 of the 19). The caller reads the real version from the
      running binary (see ``UserAgentCache``), so the version, the brands and
      the platform in UA-CH stay the browser's own.

    Deliberately unchanged: ``--enable-automation`` (its infobar is how a person
    at the shared window is told what is happening) and the service-worker block.

    ``guard_backend="cdp"`` launches differently on purpose. Chrome also
    gets ``--remote-debugging-port=0``, the second DevTools connection that
    ``cdp_guard.py`` judges documents over, and Playwright's
    ``--enable-unsafe-swiftshader`` is dropped. DataDome (etsy.com) refuses a browser
    that has either ``context.route`` (its cache-disable is enough) or that switch on
    a host with no GPU; with neither it lets the browser in.

    ``profile_dir`` is the profile this launch uses; the configured one unless
    the session had to step aside for another server (see ``ProfileLock``).
    """
    width, height = config.viewport_width, config.viewport_height
    kwargs: dict = {
        "user_data_dir": str(profile_dir or config.profile_dir),
        "headless": config.headless,
        "args": ["--disable-blink-features=AutomationControlled"],
        # Service workers intercept requests before routing sees them, which
        # would leave a hole exactly where the guard is supposed to look.
        "service_workers": "block",
    }
    if config.headless:
        kwargs["viewport"] = {"width": width, "height": height}
        kwargs["screen"] = {
            "width": max(width, _HEADLESS_SCREEN[0]),
            "height": max(height, _HEADLESS_SCREEN[1]),
        }
        if user_agent:
            kwargs["user_agent"] = user_agent
    else:
        kwargs["no_viewport"] = True
        kwargs["args"].append(f"--window-size={width},{height}")
    if config.guard_backend == "cdp":
        kwargs["args"].append("--remote-debugging-port=0")
        kwargs["ignore_default_args"] = list(_CDP_DROPPED_DEFAULTS)
    if channel:
        kwargs["channel"] = channel
    if config.proxy:
        kwargs["proxy"] = {"server": config.proxy}
    return kwargs


# Substrings Playwright uses when a channel/binary is not installed. Matching one
# of these means "try the next candidate"; anything else is a real error we surface.
_MISSING_MARKERS = (
    "is not found",
    "executable doesn't exist",
    "looks like playwright",
    "chromium distribution",
    "no such file or directory",
)


class BrowserUnavailable(Exception):
    """No usable browser could be launched from any candidate."""

    def __init__(self, tried: list[str | None], cause: Exception | None) -> None:
        self.tried = [c or "bundled-chromium" for c in tried]
        self.cause = cause
        super().__init__(f"No usable browser. Tried: {', '.join(self.tried)}")

    def envelope(self) -> dict:
        return {
            "status": "browser_unavailable",
            "tried": self.tried,
            "message": (
                "No usable browser was found to drive. Install Google Chrome so "
                "VEGA can open a window — no other setup is needed."
            ),
            "user_action": "Install Google Chrome from https://www.google.com/chrome/",
            "dev_hint": "Developers can instead run: python -m playwright install chromium",
        }


class BrowserClosed(Exception):
    """The browser was started and has since been closed, and the caller may not reopen it.

    What ``page(revive=False)`` raises. Reading is not driving: a read that put a
    window back on the screen the user had just closed would be acting on their
    browser, so it is told instead, and only a claiming call opens a new one.
    """

    def envelope(self) -> dict:
        return {
            "status": "browser_closed",
            "hint": "The browser window was closed. Call open_browser to start a new one.",
        }


class GuardLost(Exception):
    """The navigation guard stopped working, and the window was closed instead.

    Only the ``cdp`` backend can lose its guard: its second DevTools connection ended
    or failed while the browser ran. Chrome releases every request it was holding the
    moment that happens, so a browser kept open would be running unguarded; the session
    closes it, and every call answers this until ``close_browser`` acknowledges it.
    ``reason`` is fixed vocabulary — the debugging endpoint never appears in it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"the navigation guard was lost: {reason}")
        self.reason = reason

    def envelope(self) -> dict:
        return {
            "status": "guard_lost",
            "reason": (
                "The navigation guard stopped working, so the browser window was closed "
                "instead of being left open unguarded."
            ),
            "detail": self.reason,
            "hint": (
                "Call close_browser, then open_browser for a fresh window. Requests that "
                "were already in flight when the guard stopped were not judged: check "
                "the audit trail."
            ),
        }


def _is_missing_browser(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _MISSING_MARKERS)


# Substrings Playwright uses when the thing it was talking to is gone — a page, a
# context, the whole browser. Matched on the message, not the class, because
# patchright carries its own error types and a class check would miss one driver.
_CLOSED_MARKERS = (
    "has been closed",
    "target closed",
    "browser closed",
    "connection closed",
)


def _is_closed_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _CLOSED_MARKERS)


# What Chrome reports when another process already runs on the profile: it hands
# the launch to that process and exits, and Playwright surfaces the exit.
_PROFILE_BUSY_MARKERS = (
    "opening in existing browser session",
    "profile is already in use",
)


def _is_profile_busy(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _PROFILE_BUSY_MARKERS)


class ProfileLock:
    """An exclusive claim on the persistent profile, held while its browser runs.

    Chrome lets one process at a time use a profile directory, and a second
    ``launch_persistent_context`` on it fails with "Opening in existing browser
    session". Several servers share one data dir whenever the harness spawns more
    than one for the same profile (a gateway and its cron worker, say), and which
    of them opens a browser first is decided when it first needs one — long after
    any check made at spawn time. So the claim is an advisory ``flock`` taken at
    launch, in a file *beside* the profile (Chrome owns what is inside it).

    ``flock`` rather than a pid file: the kernel drops it when the holder dies,
    however it dies, so a SIGKILL leaves nothing to clean up. Where ``fcntl`` is
    missing (Windows) every claim succeeds, which is the behaviour before this
    existed.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Take the claim without waiting. False when another holder has it."""
        if self._fd is not None:
            return True
        if fcntl is None:
            return True
        try:
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True  # cannot even create the file: leave the profile unguarded
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return False
            return True  # a filesystem without flock: same as no lock at all
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing the descriptor drops the flock
            self._fd = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


def sweep_dead_instances(root: Path, keep_pid: int) -> None:
    """Delete ``root/<pid>`` for every pid that is no longer running.

    A fallback profile is private to one server, so once that server is gone the
    directory is just disk. ``keep_pid`` (this process) is never removed.
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.isdigit() or not entry.is_dir():
            continue
        pid = int(entry.name)
        if pid == keep_pid or _pid_alive(pid):
            continue
        shutil.rmtree(entry, ignore_errors=True)


class BrowserSession:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._active_channel: str | None = None
        self._active_driver: str | None = None
        self._starting = False
        # Whether a launch ever succeeded. That is what tells "never opened" from "was
        # opened and has been closed": a reader may start the first, never reopen the second.
        self._launched_once = False
        self._start_lock = asyncio.Lock()
        # Which profile the running (or last) browser uses: "shared" is the
        # configured persistent one with its saved logins, "instance" a private
        # empty one this server fell back to because another server holds the
        # shared one. Taken at launch, so a server that never opens a browser
        # never holds anything.
        self._profile_lock: ProfileLock | None = None
        self._profile_mode = "shared"
        self._profile_dir: Path = config.profile_dir
        # Serialises choosing a new active page: callers that find the same tab
        # dead together would otherwise each open a blank one.
        self._page_lock = asyncio.Lock()
        # The NavigationGuard, installed by ServerContext. Kept as a plain attribute so
        # this module does not need to know about permissions. What config.guard_backend
        # says decides how it stands in front of the browser: registered as the context's
        # route handler (and its request listener, which is how a redirect hop is heard
        # of there), or asked by the CDP sidecar (cdp_guard.py) for every document.
        self.guard: Any = None
        # The sidecar of the running browser, with the cdp backend.
        self._cdp_guard: CdpGuard | None = None
        # Set once the sidecar was lost while the browser ran: the window is closed and
        # every call is refused until stop() acknowledges it.
        self._guard_lost: GuardLost | None = None
        self._loss_task: asyncio.Task | None = None
        # Answers, records and reports the native dialogs of the browser context
        # (its listener is registered at launch). Held here: the session owns that window.
        self.dialogs = DialogHandler()
        # Installed by ServerContext (downloads.py): judges each file a tab is
        # offered. A plain attribute, like route_handler, so this module need not
        # know about permissions.
        self.download_handler: object | None = None
        # Installed by UAT mode (uat/observe.py): each is called with every tab the
        # session adopts, so a run can listen to its requests and console. A plain
        # list of callables, like download_handler, so this module need not know why.
        self.page_observers: list[Any] = []
        # What an observer raised, if any: an observer must not take the tab down,
        # but a listener that silently never attached would be a quiet lie in the run.
        self.observer_errors: list[str] = []

    @property
    def started(self) -> bool:
        return self._context is not None

    @property
    def guard_lost(self) -> GuardLost | None:
        """Why the browser was closed instead of left unguarded, until ``stop`` acknowledges it."""
        return self._guard_lost

    @property
    def live(self) -> bool:
        """Open, or on its way to being open.

        ``started`` alone is False for the several seconds a browser takes to
        launch. Anything that reads "no window, so no holder" from it hands the
        session away mid-launch, to a client that then owns the window the first
        one is still waiting for.

        A guard that was lost keeps the session ``live``: the window is gone, but the
        holder has not been told, and nobody else may take the browser before they are.
        """
        return self._context is not None or self._starting or self._guard_lost is not None

    @property
    def profile_mode(self) -> str:
        """``shared`` (saved logins) or ``instance`` (private, empty profile)."""
        return self._profile_mode

    @property
    def active_channel(self) -> str | None:
        """Which browser actually launched (channel name, or None for bundled)."""
        return self._active_channel

    async def start(self) -> None:
        """Launch the visible window, trying installed browsers before bundled Chromium.

        Raises BrowserUnavailable if no candidate can launch, and GuardLost if the
        navigation guard cannot be started or was lost.
        """
        async with self._start_lock:
            # Two callers reaching the check together would both launch against
            # the same user_data_dir; the lock makes the check-then-launch one step.
            if self._guard_lost is not None:
                raise self._guard_lost
            if self._context is not None:
                return
            self._starting = True
            try:
                await self._launch()
                self._launched_once = True
            finally:
                self._starting = False

    @property
    def active_driver(self) -> str | None:
        """Which Playwright launched the window: ``patchright`` or ``playwright``."""
        return self._active_driver

    async def _start_playwright(self) -> Playwright:
        """The one place a driver is imported, so tests can stand in for it."""
        name, async_playwright = load_driver(self._config.driver)
        self._active_driver = name
        return await async_playwright().start()

    def _claim_profile(self) -> None:
        """Decide, now that a browser is about to open, which profile it gets."""
        if self._profile_lock is None:
            self._profile_lock = ProfileLock(self._config.data_dir / _PROFILE_LOCK_NAME)
        if self._profile_lock.try_acquire():
            self._profile_mode = "shared"
            self._profile_dir = self._config.profile_dir
        else:
            self._use_instance_profile()

    def _use_instance_profile(self) -> None:
        """Step aside to a private profile: another server holds the shared one.

        Logins live in the shared profile, so this one starts empty — callers are
        told (``open_browser``) rather than left to find out at a login wall.
        """
        assert self._profile_lock is not None
        self._profile_lock.release()  # the shared profile is not ours to hold
        root = self._config.instances_dir
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        sweep_dead_instances(root, os.getpid())
        profile = root / str(os.getpid()) / "profile"
        profile.mkdir(parents=True, exist_ok=True)
        self._profile_dir = profile
        self._profile_mode = "instance"

    def _release_profile(self) -> None:
        if self._profile_lock is not None:
            self._profile_lock.release()

    def _drop_instance_profile(self) -> None:
        """A private profile holds no logins worth keeping once its window is closed."""
        if self._profile_mode == "instance":
            shutil.rmtree(self._profile_dir.parent, ignore_errors=True)

    def _launch_options(self, channel: str | None, user_agent: str | None) -> dict:
        """``launch_kwargs`` for the profile this launch uses, with the profile made ready.

        The cdp backend reads the debugging endpoint Chrome writes into the profile, so a
        file left there by the last run must be gone before this one starts.
        """
        if self._config.guard_backend == "cdp":
            prepare_profile(self._profile_dir)
        return launch_kwargs(self._config, channel, user_agent, self._profile_dir)

    async def _launch_context(self, channel: str | None, user_agent: str | None):
        """``launch_persistent_context`` on the claimed profile.

        The claim only covers servers that take it. A Chrome already on the
        shared profile that holds no claim — a server from before it existed, or
        one orphaned when its parent was killed — still fails the launch with
        "Opening in existing browser session"; that sends us to a private profile
        too, once.
        """
        assert self._pw is not None
        try:
            return await self._pw.chromium.launch_persistent_context(
                **self._launch_options(channel, user_agent)
            )
        except Exception as exc:  # noqa: BLE001 — anything else is re-raised
            if self._profile_mode != "shared" or not _is_profile_busy(exc):
                raise
            self._use_instance_profile()
            return await self._pw.chromium.launch_persistent_context(
                **self._launch_options(channel, user_agent)
            )

    async def _launch(self) -> None:
        self._config.ensure_dirs()
        self._claim_profile()
        try:
            await self._launch_claimed()
        except BaseException:
            if self._context is None:
                self._release_profile()
            raise

    async def _launch_claimed(self) -> None:
        self._pw = await self._start_playwright()
        cache = UserAgentCache(self._config.data_dir / _UA_CACHE_NAME)

        candidates = self._config.browser_candidates()
        last_error: Exception | None = None
        override: str | None = None
        for channel in candidates:
            override = cache.get(channel or "bundled") if self._config.headless else None
            try:
                self._context = await self._launch_context(channel, override)
                self._active_channel = channel
                break
            except Exception as exc:  # noqa: BLE001 — re-raised below unless "not installed"
                if not _is_missing_browser(exc):
                    await self._pw.stop()
                    self._pw = None
                    raise
                last_error = exc  # this browser isn't installed; try the next one

        if self._context is None:
            await self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(candidates, last_error)

        if self._config.headless:
            try:
                await self._settle_headless_user_agent(cache, override)
            except Exception:
                await self._teardown()
                raise

        # One dialog listener for the whole window, before any tab can raise a dialog.
        # On the context, not on each tab: a popup may ask from its first script,
        # ahead of the "page" event, and a listener added to the tab is too late.
        self._context.on("dialog", self.dialogs.handle)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        await self._install_guard()
        self._context.on("page", self._adopt_page)
        # Tabs open before that listener existed never raise a "page" event, and one
        # that predates the download listener would keep every file it is offered, unjudged.
        for page in self._context.pages:
            self._watch_downloads(page)
            self._observe(page)

    async def _install_guard(self) -> None:
        """Put the navigation guard in front of every document the browser will request.

        Before any tool navigates, and on the window that is actually open (after the
        headless UA relaunch, not before it).
        """
        assert self._context is not None
        if self.guard is None:
            return
        if self._config.guard_backend != "cdp":
            # context.route, not page.route: page.route never sees a popup's
            # first request, and opening a new tab is the most ordinary way to
            # leave a site.
            await self._context.route("**/*", self.guard)
            observer = getattr(self.guard, "on_request", None)
            if observer is not None:
                # On the context too, for the same reason: one listener hears every tab,
                # popups included. Playwright continues a redirect hop itself, so the route
                # never hears of one; this is how this backend learns of it, after it has
                # been sent. The cdp backend is shown every hop before it leaves and must
                # not also listen: each hop would be judged twice.
                self._context.on("request", observer)
            return
        sidecar: CdpGuard = CdpGuard(
            self.guard, on_lost=lambda reason: self._on_guard_lost(sidecar, reason)
        )
        try:
            await sidecar.start(self._profile_dir)
        except BaseException as exc:
            # The window is open and nothing is guarding it. Closing it is the only
            # answer that is not "carry on unguarded".
            if isinstance(exc, CdpGuardError):
                self.guard.record_failure("guard_lost", str(exc))
            await self._teardown()
            if isinstance(exc, CdpGuardError):
                raise GuardLost(str(exc)) from None
            raise
        self._cdp_guard = sidecar

    def _on_guard_lost(self, sidecar: CdpGuard, reason: str) -> None:
        """The sidecar's connection ended or failed. Called from its reader: only schedules."""
        if sidecar is self._cdp_guard and self._loss_task is None:
            self._loss_task = asyncio.ensure_future(self._handle_guard_loss(sidecar, reason))

    async def _handle_guard_loss(self, sidecar: CdpGuard, reason: str) -> None:
        """Close the browser that nothing is guarding, unless it was closing by itself.

        The socket ends in two situations that look alike: the browser quit (the user
        closed the last window, Chrome crashed, something killed it) and the guard failed
        while the browser kept running. Only the second is a loss, and Chrome has already
        released every request it was holding, so each moment spent finding out is a moment
        a page can send unjudged requests (measured: one every 4 ms from a hostile page
        while this waited 300 ms for the process to leave). What the session knows at
        this instant tells the cases apart without waiting (measured, headless and headful):

        - the process is already gone: a crash or a kill. Nothing to close.
        - it is running and the sidecar has no page attached: a browser that quits detaches
          every page over the connection before the socket ends (the user closed the last
          window), so it is closing. No page can send anything: it gets ``EXIT_GRACE_S``.
        - it is running with a page still attached: nothing explains the socket, and that
          tab is unguarded. Killed now, with no grace.
        """
        try:
            if await sidecar.browser_exited(0):
                return
            if not sidecar.has_pages() and await sidecar.browser_exited(EXIT_GRACE_S):
                return
            if sidecar is not self._cdp_guard:
                return  # an orderly stop or a relaunch got here first
            # In this order. The flag first: from here no call may be told the browser is
            # fine. Then the kill, because every moment it runs is a moment unjudged
            # requests can leave; only then the tidy close.
            self._guard_lost = GuardLost(reason)
            sidecar.kill_browser()
            self.guard.record_failure("guard_lost", reason)
            async with self._start_lock:
                await self._teardown(lost=True)
        except Exception:  # noqa: BLE001 — the flag is set; nothing here may raise into the loop
            pass
        finally:
            self._loss_task = None

    async def _settle_headless_user_agent(
        self, cache: UserAgentCache, override: str | None
    ) -> None:
        """Make the headless UA the one a windowed Chrome would send.

        Asks the running binary for its real UA, and relaunches with the fixed
        one only when the override we started with is missing or stale (a
        Chrome update). In the steady state this is one CDP call, no relaunch.
        """
        assert self._context is not None and self._pw is not None
        real = await self._real_user_agent()
        if real is None:
            return  # could not ask; keep the session rather than lose it
        wanted = _fixed_user_agent(real)
        if wanted == override:
            return
        channel = self._active_channel
        cache.put(channel or "bundled", wanted)
        await self._context.close()
        self._context = await self._launch_context(channel, wanted)

    async def _real_user_agent(self) -> str | None:
        """The UA the binary reports for itself, unaffected by any override."""
        assert self._context is not None
        try:
            pages = self._context.pages
            page = pages[0] if pages else await self._context.new_page()
            cdp = await self._context.new_cdp_session(page)
            try:
                version = await cdp.send("Browser.getVersion")
            finally:
                await cdp.detach()
        except Exception:  # noqa: BLE001 — an old build, a fake, a closed target
            return None
        user_agent = version.get("userAgent")
        return user_agent if isinstance(user_agent, str) else None

    def _adopt_page(self, page: Page) -> None:
        """Follow onto a newly opened tab.

        Without this the agent keeps reading the page it opened from while the
        real work happens in a tab nobody watches and nothing audits. The tab
        keeps its opener, which is where the agent goes back to when it closes
        (see ``_fallback_page``).
        """
        self._watch_downloads(page)
        self._observe(page)
        self._page = page

    def _observe(self, page: Page) -> None:
        """Hand a tab to every registered observer. Best effort, like the download
        listener, and like it per tab: a popup arrives through ``_adopt_page``."""
        for observer in self.page_observers:
            try:
                observer(page)
            except Exception as exc:  # noqa: BLE001 — recorded, never fatal for the tab
                self.observer_errors.append(f"{type(exc).__name__}: {exc}")

    def _watch_downloads(self, page: Page) -> None:
        """Send the files this tab is offered to ``download_handler``.

        Per tab rather than ``context.on('download')``: that event only exists
        from Playwright 1.60 and this project supports 1.58, where it never fires
        — every download would go ahead unjudged. A popup is a tab like any
        other and arrives through ``_adopt_page``.
        """
        if self.download_handler is None:
            return
        try:
            page.on("download", self.download_handler)
        except Exception:  # noqa: BLE001 — a test double, a page already gone
            # Best effort, like the dialog listener. The cost is bounded: a
            # download nobody judges stays in the driver's temp dir and is deleted
            # when the window closes — it never reaches ``download_dir``.
            pass

    def open_pages(self) -> list[Page]:
        """Every open tab, oldest first — the order ``tabs`` numbers them in."""
        if self._context is None:
            return []
        return [p for p in self._context.pages if not p.is_closed()]

    def tab_info(self) -> dict[str, int | None]:
        """How many tabs are open and which one the agent is working in."""
        pages = self.open_pages()
        active = next((i for i, p in enumerate(pages) if p is self._page), None)
        return {"tab_count": len(pages), "active_index": active}

    async def page(self, *, revive: bool = True) -> Page:
        """Return the active page, starting the browser on first use.

        Never a closed one. A tab can end under the agent — an OAuth popup that
        finishes with ``window.close()``, a tab the user shut — and a dead page
        fails every tool after it with "Target closed" while ``open_browser``
        still reports success. Every tool gets its page from here, so this is
        where it is checked and, when it is gone, replaced.

        Replaced by a tab that is already open where there is one: moving the
        pointer puts nothing on the screen. Only a caller that may ``revive`` the
        browser gets a new tab, or a new browser, when none is left. A reader
        passes ``revive=False`` and gets ``BrowserClosed`` instead — whether the
        user shut the last tab, the browser quit or crashed, or it was closed on
        purpose — because a read must not undo that. A browser nobody has opened
        yet still opens on first use, readers included.
        """
        await self._settle_guard()
        if not revive and self._context is None and self._launched_once:
            raise BrowserClosed()
        if self._page is None:
            await self.start()  # a fresh launch hands back a live page
            if self._page is None:
                # Started earlier, and every tab has closed since.
                await self._recover_page(revive=revive)
        elif self._page.is_closed():
            await self._recover_page(revive=revive)
        assert self._page is not None  # start() or _recover_page() guarantees a page or raises
        return self._page

    async def _settle_guard(self) -> None:
        """Refuse every call once the guard was lost — after finding out whether it was.

        A sidecar whose socket just ended is still being told apart from a browser that
        just quit (``_handle_guard_loss``); a call that arrives meanwhile waits for that
        verdict rather than guess.
        """
        if self._loss_task is not None:
            await asyncio.shield(self._loss_task)
        if self._guard_lost is not None:
            raise self._guard_lost

    async def activate(self, page: Page) -> bool:
        """Make ``page`` the tab the agent works in, and bring it to the front so the
        user is looking at what the agent is. False when it closed first."""
        if page.is_closed():
            return False
        self._page = page
        return await self._to_front(page)

    async def close_page(self, page: Page) -> Page:
        """Close one tab and return the tab the agent is working in afterwards.

        The browser is never left with none. Closing headful Chrome's last window
        quits the browser and the context with it (measured), so when this is the
        last tab a blank one is opened first and the close comes after. Closing
        the tab the agent is on hands it to that tab's opener, else the newest
        other tab — the same choice as when a tab closes by itself.
        """
        async with self._page_lock:
            if not page.is_closed():
                others = [p for p in self.open_pages() if p is not page]
                leaving = page is self._page
                if not others:
                    await self._open_blank_page()
                elif leaving:
                    self._page = await self._fallback_page(page)
                await page.close()
                if leaving and self._page is not None:
                    await self._to_front(self._page)
        return await self.page()

    async def _recover_page(self, *, revive: bool) -> None:
        """Move onto a live tab after the active one went away.

        Where to land, in order: the tab that opened it — a popup that closes
        itself hands the user back to the page they were on, script state and
        all — then the newest open tab, then, for a caller that may ``revive``,
        a fresh blank one. Re-checked once the lock is held, so callers that find
        the same dead tab together open one blank tab between them, not one each.
        """
        async with self._page_lock:
            if self._page is not None and not self._page.is_closed():
                return
            if self._context is None:
                if not revive:
                    raise BrowserClosed()
                await self.start()  # closed while we waited; this launches afresh
                return
            replacement = await self._fallback_page(self._page)
            if replacement is None:
                if not revive:
                    raise BrowserClosed()
                replacement = await self._open_blank_page()
            # A tab adopted while we looked is the newer answer; do not undo it.
            if self._page is None or self._page.is_closed():
                self._page = replacement

    async def _fallback_page(self, gone: Page | None) -> Page | None:
        """The tab to go back to once ``gone`` is no longer the agent's: whichever
        tab opened it, else the newest other open tab, else None."""
        if gone is not None:
            try:
                opener = await gone.opener()
            except Exception:  # noqa: BLE001 — no answer means no opener to return to
                opener = None
            if opener is not None and not opener.is_closed():
                return opener
        others = [p for p in self.open_pages() if p is not gone]
        return others[-1] if others else None

    async def _open_blank_page(self) -> Page:
        """Open an empty tab and make it the active one.

        When the browser itself is gone — its last window was closed and headful
        Chrome quit with it, or it crashed — there is nothing to open a tab in,
        and starting again is all that leaves the agent a page that works.
        """
        assert self._context is not None
        try:
            return await self._context.new_page()
        except Exception as exc:  # noqa: BLE001 — anything else is re-raised below
            if not _is_closed_error(exc):
                raise
        await self.stop()
        await self.start()
        assert self._page is not None  # start() guarantees a page or raises
        return self._page

    @staticmethod
    async def _to_front(page: Page) -> bool:
        """Raise a tab so the user sees the one the agent is on. False if it closed."""
        try:
            await page.bring_to_front()
        except Exception as exc:  # noqa: BLE001 — anything else is re-raised below
            if _is_closed_error(exc):
                return False
            raise
        return True

    async def stop(self) -> None:
        """Close the window and release Playwright resources.

        Takes the launch lock: a close racing a launch would otherwise return
        while the window it meant to shut was still being opened, leaving one
        standing that nobody believes in.

        Also what acknowledges a lost guard: the next ``start`` is a fresh, guarded one.
        """
        async with self._start_lock:
            await self._teardown()

    async def _teardown(self, *, lost: bool = False) -> None:
        """Close the window, release Playwright, and stop the sidecar once the browser is gone.

        ``lost``: the guard failed and the browser may already be dead or hung, so
        nothing here may raise and the lost marker is kept. Otherwise this is an
        ordinary close, and clears the marker.
        """
        sidecar, self._cdp_guard = self._cdp_guard, None
        if sidecar is not None:
            # The browser is about to go and take the socket with it: not a loss.
            sidecar.begin_close()
        try:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception as exc:  # noqa: BLE001 — anything else is re-raised below
                    # Closing a browser that already went away can report just that.
                    # The aim is a closed context, so it is not a failure.
                    if not (lost or _is_closed_error(exc)):
                        raise
                self._context = None
                self._page = None
                self._active_channel = None
                self._active_driver = None
                self.dialogs.reset()
            if self._pw is not None:
                try:
                    await self._pw.stop()
                except Exception:  # noqa: BLE001 — a lost browser may take the driver's pipes
                    if not lost:
                        raise
                self._pw = None
            self._release_profile()
            self._drop_instance_profile()
        finally:
            if sidecar is not None:
                await sidecar.stop()
        if not lost:
            self._guard_lost = None
