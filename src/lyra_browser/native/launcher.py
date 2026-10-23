"""Find a Chrome, start it the way Playwright would, and hand back its DevTools endpoint.

The switch list is Playwright's own, measured from the command line of a browser that
Playwright 1.64 launched (``testing/native_cdp_spike_261022_v1.py``), because each one
changes something lyra depends on:

- ``--use-mock-keychain`` / ``--password-store=basic``: the shared profile's cookies are
  encrypted with a key these choose. Without them a profile written under Playwright
  cannot be read back and every site logs the user out (measured: with them, a cookie
  written under Playwright is read under this launcher).
- ``--disable-back-forward-cache``: a back/forward-cache restore sends no request, so
  ``cdp_guard`` would never see it.
- ``--disable-popup-blocking``: a tab the page opens has to open for the agent to follow.

Two deliberate differences from Playwright:

- the sandbox stays on. Playwright passes ``--no-sandbox``; this browser holds the
  user's real logins, and a renderer exploit should not reach them. ``chromium_sandbox``
  false (a container that cannot sandbox) puts the switch back;
- the endpoint is a ``--remote-debugging-port=0`` read from ``DevToolsActivePort``, not
  Playwright's pipe, so ``cdp_guard`` and this driver can share it. The launcher is the
  one reader of that file (it deletes it), and hands the endpoint to both.

Error messages keep the substrings ``session`` reads: ``is not found`` (try the next
browser), ``profile is already in use`` (the profile is taken; see
``_HELD_PROFILE_MARKERS`` for how that is recognised).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..cdp_guard import read_endpoint
from .errors import Error, TargetClosedError

# Playwright 1.64's chromium defaults, headful (see module docstring), minus the
# per-launch switches added below and minus --no-sandbox.
DEFAULT_SWITCHES: tuple[str, ...] = (
    "--disable-field-trial-config",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-back-forward-cache",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-component-update",
    "--no-default-browser-check",
    "--disable-default-apps",
    "--disable-dev-shm-usage",
    "--disable-edgeupdater",
    "--disable-extensions",
    "--disable-features=AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,"
    "DialMediaRouteProvider,GlobalMediaControls,HttpsUpgrades,LensOverlay,MediaRouter,"
    "PaintHolding,ThirdPartyStoragePartitioning,BlockOriginHeaderModificationOnRedirect,"
    "AvoidCorsURLLoaderRestartOnRedirect,Translate,AutoDeElevate,OptimizationHints,"
    "NetworkTimeServiceQuerying,AimEnabled,msForceBrowserSignIn,"
    "msEdgeUpdateLaunchServicesPreferredVersion",
    "--enable-features=CDPScreenshotNewSurface",
    "--allow-pre-commit-input",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-updater-scheduler",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--no-service-autorun",
    "--export-tagged-pdf",
    "--disable-search-engine-choice-screen",
    "--unsafely-disable-devtools-self-xss-warnings",
    "--edge-skip-compat-layer-relaunch",
    "--disable-infobars",
    "--disable-sync",
)
HEADLESS_SWITCHES: tuple[str, ...] = (
    "--headless",
    "--hide-scrollbars",
    "--mute-audio",
    "--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,"
    "availablePointerTypes=4",
)
NO_SANDBOX = "--no-sandbox"
DEBUGGING_PORT = "--remote-debugging-port=0"

START_TIMEOUT_S = 30.0
_OUTPUT_LINES = 40
_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _channel_paths(channel: str | None) -> list[Path]:
    """Where ``channel`` is installed on this platform, most likely first."""
    home = Path.home()
    if sys.platform == "darwin":
        apps = {
            "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
            "chrome-beta": "Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "msedge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "chromium": "Chromium.app/Contents/MacOS/Chromium",
        }
        rel = apps.get(channel or "")
        return [Path("/Applications") / rel, home / "Applications" / rel] if rel else []
    if sys.platform.startswith("win"):
        rels = {
            "chrome": r"Google\Chrome\Application\chrome.exe",
            "chrome-beta": r"Google\Chrome Beta\Application\chrome.exe",
            "msedge": r"Microsoft\Edge\Application\msedge.exe",
            "chromium": r"Chromium\Application\chrome.exe",
        }
        rel = rels.get(channel or "")
        roots = [os.environ.get(k) for k in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)")]
        return [Path(root) / rel for root in roots if root] if rel else []
    linux = {
        "chrome": [
            "/opt/google/chrome/chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome",
        ],
        "chrome-beta": ["/opt/google/chrome-beta/chrome", "/usr/bin/google-chrome-beta"],
        "msedge": ["/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge-stable"],
        "chromium": ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium"],
    }
    return [Path(p) for p in linux.get(channel or "", [])]


_CFT_APP = "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"


def _bundled_paths() -> list[Path]:
    """Chromium builds a ``playwright install chromium`` left behind, newest first."""
    explicit = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if explicit and explicit != "0":
        roots = [Path(explicit)]
    elif sys.platform == "darwin":
        roots = [Path.home() / "Library" / "Caches" / "ms-playwright"]
    elif sys.platform.startswith("win"):
        roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"]
    else:
        roots = [Path.home() / ".cache" / "ms-playwright"]
    if sys.platform == "darwin":
        rels = [
            f"chrome-mac-arm64/{_CFT_APP}",
            f"chrome-mac/{_CFT_APP}",
            "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        ]
    elif sys.platform.startswith("win"):
        rels = [r"chrome-win64\chrome.exe", r"chrome-win\chrome.exe"]
    else:
        rels = ["chrome-linux64/chrome", "chrome-linux/chrome"]
    found: list[tuple[int, Path]] = []
    for root in roots:
        for build in root.glob("chromium-*"):
            number = re.fullmatch(r"chromium-(\d+)", build.name)
            if number:
                found.extend((int(number.group(1)), build / rel) for rel in rels)
    return [path for _n, path in sorted(found, key=lambda item: -item[0])]


def find_executable(channel: str | None) -> str:
    """The browser binary for ``channel`` (None: Playwright's bundled Chromium).

    Raises ``Error`` worded with ``is not found``, which ``session`` reads as "try the
    next candidate".
    """
    candidates = _channel_paths(channel) if channel else _bundled_paths()
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    name = channel or "chromium (bundled)"
    where = ", ".join(str(p) for p in candidates) or "no known location on this platform"
    raise Error(f'Chromium distribution "{name}" is not found at {where}')


def build_args(options: dict) -> list[str]:
    """Chrome's command line for a ``launch_kwargs`` dict (``session.launch_kwargs``).

    Viewport, screen and the service-worker block are not switches; the context applies
    them per page.
    """
    ignored = set(options.get("ignore_default_args") or ())
    args = [s for s in DEFAULT_SWITCHES if s not in ignored]
    if options.get("headless"):
        args.extend(s for s in HEADLESS_SWITCHES if s not in ignored)
    if options.get("chromium_sandbox") is False:
        args.append(NO_SANDBOX)
    user_agent = options.get("user_agent")
    if user_agent:
        args.append(f"--user-agent={user_agent}")
    proxy = options.get("proxy")
    if proxy and proxy.get("server"):
        args.append(f"--proxy-server={proxy['server']}")
    extra = list(options.get("args") or ())
    if DEBUGGING_PORT not in extra:
        extra.append(DEBUGGING_PORT)
    args.extend(extra)
    args.append(f"--user-data-dir={options['user_data_dir']}")
    args.append("about:blank")
    return args


@dataclass
class BrowserProcess:
    """A launched browser: its process and the DevTools endpoint it wrote."""

    process: asyncio.subprocess.Process
    endpoint: tuple[int, str] = field(repr=False)
    output: deque = field(default_factory=lambda: deque(maxlen=_OUTPUT_LINES), repr=False)
    _drain: asyncio.Task | None = field(default=None, repr=False)

    @property
    def pid(self) -> int:
        return self.process.pid

    def exited(self) -> bool:
        return self.process.returncode is not None

    async def wait(self, timeout: float) -> bool:
        """Whether the process ended within ``timeout`` seconds."""
        try:
            await asyncio.wait_for(self.process.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def kill(self) -> None:
        if not self.exited():
            with contextlib.suppress(ProcessLookupError, OSError):
                self.process.send_signal(_KILL)

    async def reap(self) -> None:
        """Kill if still running, wait for the exit, stop reading its output."""
        self.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.process.wait(), 10)
        if self._drain is not None:
            self._drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._drain


async def _drain(stream: asyncio.StreamReader | None, sink: deque) -> None:
    """Keep the browser's output moving (a full pipe blocks it) and its tail at hand."""
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        sink.append(line.decode("utf-8", "replace").rstrip())


# How a Chrome finds another one on its profile. A windowed one hands the launch over
# and exits 0 before writing an endpoint (silently on macOS; "Opening in existing
# browser session." elsewhere); a headless one fails on the profile's SingletonLock
# (exit code 21). Measured, Chrome 155 on macOS.
_HELD_PROFILE_MARKERS = (
    "opening in existing browser session",
    "singletonlock",
    "process_singleton",
)
_PROFILE_IN_USE = " The profile is already in use by another browser."


def _names_a_held_profile(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in _HELD_PROFILE_MARKERS)


async def launch(options: dict, *, timeout: float = START_TIMEOUT_S) -> BrowserProcess:
    """Start the browser ``options`` describe and wait for its debugging endpoint.

    The profile must already have had ``cdp_guard.prepare_profile`` (no stale endpoint
    file). Raises ``Error``: ``is not found`` for a missing binary, ``profile is already
    in use`` when another running Chrome holds the profile, which makes this one exit
    instead of failing.
    """
    executable = find_executable(options.get("channel"))
    output: deque = deque(maxlen=_OUTPUT_LINES)
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *build_args(options),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise Error(f"Failed to launch the browser process: {exc.strerror or exc}") from None
    drain = asyncio.create_task(_drain(process.stdout, output))
    endpoint_task = asyncio.create_task(read_endpoint(Path(options["user_data_dir"]), timeout))
    exit_task = asyncio.create_task(process.wait())
    try:
        done, _ = await asyncio.wait(
            {endpoint_task, exit_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if endpoint_task in done and endpoint_task.exception() is None:
            return BrowserProcess(process, endpoint_task.result(), output, drain)
        if exit_task in done:
            # Let the last lines arrive; "Opening in existing browser session." is one.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(drain), 1.0)
            tail = "\n".join(output)
            held = process.returncode == 0 or _names_a_held_profile(tail)
            busy = _PROFILE_IN_USE if held else ""
            raise Error(
                f"Browser closed during launch (exit code {process.returncode}).{busy}"
                + (f"\n{tail}" if tail else "")
            )
        raise Error("Timed out waiting for the browser to start")
    except BaseException:
        for task in (endpoint_task, exit_task):
            task.cancel()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.send_signal(_KILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), 10)
        drain.cancel()
        raise
    finally:
        for task in (endpoint_task, exit_task):
            if task.done() and not task.cancelled():
                task.exception()  # retrieved, so it is never reported as unhandled
            elif not task.done():
                task.cancel()


def closed_error(process: BrowserProcess) -> TargetClosedError:
    return TargetClosedError(
        "Target page, context or browser has been closed"
        + (f" (browser exited with code {process.process.returncode})" if process.exited() else "")
    )


__all__ = [
    "BrowserProcess",
    "build_args",
    "closed_error",
    "find_executable",
    "launch",
]
