"""Test doubles that let tool bodies run without a real browser.

Every tool obtains its page through ``acquire_page(ctx)``, so swapping the
session inside the ServerContext is enough to exercise a whole tool body — its
gates, its audit record, its envelope — with no Chromium and no network. Before
this harness the suite only checked that tools were *registered*; nothing ever
ran one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

from lyra_browser.approval import CollaborationState
from lyra_browser.audit import AuditLog
from lyra_browser.config import Config
from lyra_browser.context import ServerContext
from lyra_browser.dialogs import DialogHandler
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability
from lyra_browser.tools import register_all

# The tool module's own scripts, so a fake can answer the same question a real
# page would. Imported rather than duplicated: a copy that drifts would let a
# test pass on a script the tools no longer send.
from lyra_browser.tools.forms import (
    _AFTER_SEND_JS,
    _DRAFT_JS,
    _FORM_JS,
    _IS_FRAME_JS,
    _PUBLISH_STATE_JS,
    _SET_HTML_JS,
    _SYNC_EDITOR_JS,
)
from lyra_browser.tools.interaction import _SCROLL_STATE_JS, _SCROLL_TO_JS

# A 2x3 PNG: signature + IHDR only, enough for png_size() and a file on disk.
FAKE_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\x0dIHDR"
    b"\x00\x00\x00\x02\x00\x00\x00\x03\x08\x06\x00\x00\x00"
    b"\x00\x00\x00\x00"
)


class FakeLocator:
    """Stands in for a Playwright Locator and records what was asked of it."""

    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector
        self.first = self

    async def click(self, timeout: float | None = None) -> None:
        self._page.calls.append(("click", self._selector))
        # A refused form reports itself in a dialog as the click lands.
        for message in self._page.dialogs_on_click:
            self._page.raise_dialog(message)
        self._page.dialogs_on_click = []
        self._page.start_download(("click", self._selector))
        self._page.react(("click", self._selector))

    async def hover(self, timeout: float | None = None) -> None:
        self._page.calls.append(("hover", self._selector))
        self._page.react(("hover", self._selector))

    async def scroll_into_view_if_needed(self, timeout: float | None = None) -> None:
        self._page.calls.append(("scroll_into_view", self._selector))

    async def fill(self, value: str, timeout: float | None = None) -> None:
        self._page.calls.append(("fill", self._selector, value))
        self._page.react(("fill", self._selector))

    async def press(self, key: str, timeout: float | None = None) -> None:
        self._page.calls.append(("press", self._selector, key))
        self._page.react(("press", self._selector, key))

    async def get_attribute(self, name: str) -> str | None:
        return self._page.attributes.get(name)

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        # Only _looks_like_submit uses this: "is this a bare <button> in a form?"
        self._page.calls.append(("evaluate", self._selector, expression, arg))
        if expression == _IS_FRAME_JS:
            return self._selector in self._page.frames
        if expression == _SET_HTML_JS:
            self._page.written = arg
            # A CKEditor body: setData() fills the editor, updateElement() fills
            # the field the form posts. Reporting both is what lets a caller see
            # an editor that looks filled while the form would send nothing.
            return {
                "written": len(arg or ""),
                "synced": self._page.synced_editor,
                "syncedField": "news" if self._page.synced_editor else "",
                "fieldChars": len(arg or "") if self._page.synced_editor else 0,
            }
        if expression == _SYNC_EDITOR_JS:
            self._page.calls.append(("sync_editor", self._selector))
            # The sync path always finds the instance on a real CKEditor page;
            # what varies is how many chars reached the posted field.
            return {
                "synced": self._page.editor_name,
                "syncedField": self._page.editor_name,
                "fieldChars": self._page.editor_field_chars,
                "editorChars": self._page.editor_field_chars,
            }
        return self._page.is_form_button

    async def inner_text(self) -> str:
        return self._page.text

    async def count(self) -> int:
        return 1 if self._page.has(self._selector) else 0

    async def screenshot(self) -> bytes:
        self._page.calls.append(("element_screenshot", self._selector))
        return self._page.png

    async def select_option(self, *, value=None, label=None, index=None) -> None:
        self._page.calls.append(("select_option", self._selector, value, label, index))
        self._page.selected = value or label or ""
        if index is not None and index >= 0:
            self._page.selected = str(index)

    async def input_value(self) -> str:
        return self._page.selected

    async def set_input_files(self, files) -> None:
        self._page.calls.append(("set_input_files", self._selector, list(files)))
        self._page.attached = list(files)

    async def check(self) -> None:
        self._page.calls.append(("check", self._selector))
        self._page.publish_checked = self._selector
        # Switching the control is what makes the page report the live state.
        self._page.published_state = self._page.live_state

    async def is_checked(self) -> bool:
        return self._page.publish_checked == self._selector

    async def is_visible(self) -> bool:
        return self._page.has(self._selector) and self._selector not in self._page.hidden

    async def is_enabled(self) -> bool:
        return self._page.has(self._selector) and self._selector not in self._page.disabled


class FakeFrameLocator:
    """Stands in for a frame_locator(): resolves to that frame's ``body``."""

    def __init__(self, page: FakePage, frame_selector: str) -> None:
        self._page = page
        self._frame_selector = frame_selector

    def locator(self, selector: str) -> FakeLocator:
        self._page.calls.append(("frame_locator", self._frame_selector, selector))
        return FakeLocator(self._page, selector)


class FakeDialog:
    """A native dialog: what the session's context listener receives."""

    def __init__(
        self, message: str, type: str = "alert", default_value: str = "", page=None
    ) -> None:
        self.message = message
        self.type = type
        self.default_value = default_value
        # The tab that raised it; ``FakePage.raise_dialog`` fills this in.
        self.page = page
        self.dismissed = False
        # Every answer given, in order. A dialog answered twice is a driver error
        # in real life, so a test can insist on exactly one.
        self.answers: list[tuple] = []

    def dismiss(self) -> None:
        self.dismissed = True
        self.answers.append(("dismiss",))

    def accept(self, prompt_text: str | None = None) -> None:
        self.answers.append(("accept", prompt_text))


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def press(self, key: str) -> None:
        self._page.calls.append(("key", key))
        self._page.start_download(("key", key))
        self._page.react(("key", key))


class FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self._page.calls.append(("wheel", delta_x, delta_y))
        if not self._page.scroll_locked:
            moved = self._page.scroll_y + delta_y
            self._page.scroll_y = int(max(0, min(moved, self._page.scroll_max)))


class FakePage:
    """A Playwright Page stand-in whose every mutation lands in ``calls``.

    Tests assert on ``calls`` to prove a gated action did *nothing* — returning
    the right envelope is only half of a gate; not touching the page is the
    other half.
    """

    def __init__(self, url: str = "https://start.example/", title: str = "Start") -> None:
        self.url = url
        self._title = title
        self.calls: list[tuple] = []
        # What get_attribute() answers — drives submit-control detection.
        self.attributes: dict[str, str] = {}
        # Whether an attribute-less <button> sits inside a form.
        self.is_form_button = False
        self.text = "page text"
        # Selectors that exist on the fake page; None means everything matches.
        self.present: set[str] | None = None
        # What captures return. A real PNG header so size parsing is exercised.
        self.png = FAKE_PNG
        # How many captures fail with Chrome's "no frame yet" before one works.
        self.frames_missing = 0
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse(self)
        # The document's scroll geometry. ``scroll_locked`` is a page whose
        # content scrolls inside a panel of its own: the window never moves.
        self.scroll_y = 0
        self.scroll_height = 5000
        self.viewport_height = 800
        self.scroll_locked = False
        # Selectors standing in for iframes, so a frame_locator() can be answered.
        self.frames: set[str] = set()
        # What select_option()/input_value() last settled on.
        self.selected = ""
        # What upload_file() attached.
        self.attached: list[str] = []
        # What set_editor() wrote, and (if any) the CKEditor instance it synced.
        self.written: Any = None
        self.synced_editor = ""
        # What read_form() sees. Shaped like the real script's return value.
        self.form_dump: dict = {
            "fields": [
                {
                    "tag": "input",
                    "type": "text",
                    "name": "inst",
                    "id": "inst",
                    "label": "Product Name",
                    "required": True,
                    "disabled": False,
                    "usable": True,
                    "value": "",
                },
                {
                    "tag": "select",
                    "type": "select-one",
                    "name": "copy_prot",
                    "id": "",
                    "label": "Copy Protection",
                    "required": False,
                    "disabled": False,
                    "usable": True,
                    "value": "0",
                    "options": [{"value": "0", "text": "Unknown"}, {"value": "9", "text": "iLok"}],
                },
            ],
            "buttons": [
                {"tag": "button", "type": "submit", "name": "", "value": "", "text": "Submit"}
            ],
            "editors": [
                {
                    "tag": "iframe",
                    "id": "",
                    "cls": "cke_wysiwyg_frame cke_reset",
                    "instance": "news",
                    "content": "",
                }
            ],
            "hiddenCount": 3,
            "fieldCount": 2,
            "drafts": [
                {"name": "is_draft", "value": "1", "checked": True, "label": "Draft"},
                {"name": "is_draft", "value": "0", "checked": False, "label": "Publish"},
            ],
            "state": ["Draft"],
            "submitText": ["Submit"],
        }
        # What read_draft() sees: a draft with content filled in.
        self.draft_dump: dict = {
            "fields": [
                {
                    "name": "head",
                    "label": "Headline",
                    "usable": True,
                    "value": "Intrect releases de-artifact",
                },
                {"name": "url", "label": "URL", "usable": True, "value": "https://intrect.io/"},
                # A section the form has not revealed — in a draft dump it must
                # still be listed, with usable=false, not omitted.
                {
                    "name": "event_country",
                    "label": "Country",
                    "usable": False,
                    "value": "Choose Country (or Online)",
                },
            ],
            "editors": [
                {
                    "instance": "news",
                    "chars": 42,
                    "content": "<p>Intrect ships de-artifact 0.3.16.</p>",
                },
            ],
            "attachments": [
                {
                    "name": "",
                    "accept": ".jpg,.gif,.png",
                    "multiple": True,
                    "selected": ["intrect-logo.png"],
                },
            ],
            "state": [{"name": "is_draft", "value": "1", "label": "Draft"}],
            "submits": [{"text": "Submit", "name": ""}],
            "fieldCount": 2,
            "isDraft": True,
        }
        # What a CKEditor instance reports: the field the form posts, in chars.
        self.editor_field_chars = 42
        self.editor_name = "news"
        # What the page says after a form is sent. Default: it was accepted, so
        # the form is gone and no reasons came back.
        self.after_send: dict = {
            "formPresent": False,
            "formAction": "",
            "fields": 0,
            "errors": [],
            "alert": "",
            "refused": False,
        }
        # Where raise_dialog() delivers: the session's dialog listener. FakeSession wires
        # it; it is not registered through ``on``, which would put a call in every
        # test's ``calls``.
        self.dialog_sink: Callable[[FakeDialog], None] | None = None
        # Every dialog raise_dialog() delivered, so a test can read how each was answered.
        self.raised_dialogs: list[FakeDialog] = []
        # Downloads a trigger starts (one, or a list of them): ("click", selector),
        # ("key", key), ("goto", url), ("go_back",) or ("reload",). Delivered once.
        self.downloads_on: dict[tuple, FakeDownload | list[FakeDownload]] = {}
        # The handler the session would have put on this tab. make_ctx wires it; it
        # is not registered through ``on`` because that would put a call in the
        # ``calls`` of every test.
        self.on_download: Callable[[FakeDownload], None] | None = None
        # Dialogs the page raises as a click lands — how a refused form reports.
        self.dialogs_on_click: list = []
        # What a trigger sets off in the page beyond the call itself, delivered once:
        # ("click", selector), ("hover", selector), ("fill", selector), ("press", selector,
        # key) or ("key", key). A test puts here what the page would do in answer - a
        # refusal counted by the guard, a tab opened.
        self.reactions: dict[tuple, Callable[[], None]] = {}
        # The page's publish state: what read_draft/publish/save_draft read.
        # A form opens on its private setting, so that is the default — and
        # check() flips it, the way switching the radio does on a real page.
        self.draft_state: list = [
            {"name": "is_draft", "value": "1", "label": "Draft"},
        ]
        self.live_state: list = [
            {"name": "is_draft", "value": "0", "label": "Publish"},
        ]
        self.published_state: list = self.draft_state
        # The selector publish() last switched, so is_checked() can answer.
        self.publish_checked = ""
        # Selectors that are in the DOM but unusable — a section hidden until its
        # type is chosen is the real case this stands in for.
        self.hidden: set[str] = set()
        self.disabled: set[str] = set()

    @property
    def scroll_max(self) -> int:
        return self.scroll_height - self.viewport_height

    def scroll_state(self) -> dict:
        return {
            "scroll_y": self.scroll_y,
            "scroll_height": self.scroll_height,
            "at_bottom": self.scroll_y >= self.scroll_max - 2,
        }

    def react(self, trigger: tuple) -> None:
        """Do what the page does in answer to ``trigger``, once."""
        reaction = self.reactions.pop(trigger, None)
        if reaction is not None:
            reaction()

    def has(self, selector: str) -> bool:
        return self.present is None or selector in self.present

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def frame_locator(self, selector: str) -> FakeFrameLocator:
        return FakeFrameLocator(self, selector)

    async def title(self) -> str:
        return self._title

    async def goto(self, url: str, wait_until: str | None = None) -> None:
        self.calls.append(("goto", url))
        if self.start_download(("goto", url)):
            raise RuntimeError(
                f'Page.goto: Download is starting\nCall log:\n  - navigating to "{url}"'
            )
        self.url = url

    async def go_back(self, wait_until: str | None = None) -> None:
        self.calls.append(("go_back",))
        if self.start_download(("go_back",)):
            raise RuntimeError("Page.go_back: Download is starting")

    async def reload(self, wait_until: str | None = None) -> None:
        self.calls.append(("reload",))
        if self.start_download(("reload",)):
            raise RuntimeError("Page.reload: Download is starting")

    def start_download(self, trigger: tuple) -> bool:
        """Start the download(s) registered for ``trigger``, the way a browser would: the
        tab's handler is called with each, at once or ``arrives_after`` seconds later."""
        found = self.downloads_on.pop(trigger, None)
        if found is None:
            return False
        for download in found if isinstance(found, list) else [found]:
            download.page = self
            if self.on_download is None:
                continue
            if download.arrives_after:
                asyncio.get_running_loop().call_later(
                    download.arrives_after, self.on_download, download
                )
            else:
                self.on_download(download)
        return True

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        self.calls.append(("evaluate", expression, arg))
        if expression == _SCROLL_STATE_JS:
            return self.scroll_state()
        if expression == _SCROLL_TO_JS:
            self.calls.append(("scroll_to", arg))
            if not self.scroll_locked:
                self.scroll_y = 0 if arg == "top" else self.scroll_max
        if expression == _FORM_JS:
            return self.form_dump
        if expression == _DRAFT_JS:
            return self.draft_dump
        if expression == _PUBLISH_STATE_JS:
            return self.published_state
        if expression == _AFTER_SEND_JS:
            self.calls.append(("after_send",))
            return self.after_send
        return True

    def wait_for_timeout(self, ms: int) -> Any:
        """The fake never really waits; the call is recorded for assertions."""
        self.calls.append(("wait_for_timeout", ms))
        return None

    def on(self, event: str, handler) -> None:
        """Record a listener being added. Nothing is delivered through it."""
        self.calls.append(("on", event))

    def raise_dialog(self, dialog: str | FakeDialog) -> FakeDialog:
        """Deliver a native dialog the way the browser does: to the session's listener.

        A bare string is an ``alert``; hand in a ``FakeDialog`` for any other kind.
        """
        if isinstance(dialog, str):
            dialog = FakeDialog(dialog)
        dialog.page = self
        self.raised_dialogs.append(dialog)
        if self.dialog_sink is not None:
            self.dialog_sink(dialog)
        return dialog

    async def screenshot(self, full_page: bool = False) -> bytes:
        self.calls.append(("screenshot", full_page))
        if self.frames_missing > 0:
            self.frames_missing -= 1
            raise RuntimeError("Page.screenshot: Protocol error: Unable to capture screenshot")
        return self.png


class FakeDownload:
    """A Playwright ``Download``: bytes that arrive, and every way a handler may deal with them.

    The browser's own copy is a real file under ``home``, so "nothing left on disk"
    is something a test can look at. ``ops`` lists what was done to the download,
    in order. It behaves the way a real one measured: ``cancel`` removes a transfer
    still in flight but ignores one that already finished, and ``delete`` on a
    cancelled download reports "canceled".
    """

    def __init__(
        self,
        home: Path,
        data: bytes = b"a,b\n1,2\n",
        *,
        name: str = "report.csv",
        url: str = "https://start.example/files/report.csv",
        arrives_after: float | None = None,
        finished: bool = True,
        error: str = "",
        hangs: bool = False,
    ) -> None:
        self.url = url
        self.suggested_filename = name
        self.page: FakePage | None = None
        self.arrives_after = arrives_after
        self.ops: list[str] = []
        self._data = data
        self._finished = finished and not hangs
        self._error = error
        self._hangs = hangs
        self._canceled = False
        home.mkdir(parents=True, exist_ok=True)
        self.artifact = home / f"artifact-{uuid.uuid4().hex}"
        self.artifact.write_bytes(data)

    async def path(self) -> Path:
        self.ops.append("path")
        if self._hangs:
            await asyncio.Event().wait()
        if self._canceled or self._error:
            raise RuntimeError(f"Download.path: {self._error or 'canceled'}")
        return self.artifact

    async def save_as(self, path) -> None:
        self.ops.append("save_as")
        if self._canceled:
            raise RuntimeError("Download.saveAs: canceled")
        Path(path).write_bytes(self._data)

    async def cancel(self) -> None:
        self.ops.append("cancel")
        if not self._finished:
            self._canceled = True
            self.artifact.unlink(missing_ok=True)

    async def delete(self) -> None:
        self.ops.append("delete")
        if self._canceled:
            raise RuntimeError("Download.delete: canceled")
        self.artifact.unlink(missing_ok=True)

    async def failure(self) -> str | None:
        return "canceled" if self._canceled else (self._error or None)


class FakeSession:
    """A BrowserSession that hands out a FakePage and never launches anything."""

    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.tabs: list[FakePage] = [page]
        """The open tabs, oldest first - what ``BrowserSession.open_pages`` reports."""
        self.active_channel = "chrome"
        self.active_driver = "playwright"
        self.started = True
        """A fake that hands out a page is, by definition, an open window."""
        self.starting = False
        # The real session's dialog handler, listening the way its context listener
        # does. Wired straight onto the page: registering through ``page.on`` would
        # put a call in every test's ``page.calls``.
        self.dialogs = DialogHandler()
        page.dialog_sink = self.dialogs.handle

    async def page(self, *, revive: bool = True) -> FakePage:
        return self._page

    def open_pages(self) -> list[FakePage]:
        return list(self.tabs)

    def tab_info(self) -> dict[str, int | None]:
        active = next((i for i, tab in enumerate(self.tabs) if tab is self._page), None)
        return {"tab_count": len(self.tabs), "active_index": active}

    def adopt(self, url: str = "about:blank") -> FakePage:
        """A tab the page opened: the session follows onto it, as ``_adopt_page`` does."""
        tab = FakePage(url)
        self.tabs.append(tab)
        self._page = tab
        return tab

    @property
    def live(self) -> bool:
        """Mirrors BrowserSession.live — open, or on its way to being open."""
        return self.started or self.starting

    async def stop(self) -> None:
        self.started = False


@pytest.fixture(autouse=True)
def _no_host_client(monkeypatch):
    """Keep the host's own harness out of client detection. A developer running
    the suite inside a Hermes shell has HERMES_HOME set, which would otherwise
    move every default path into their real ~/.hermes."""
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("LYRA_BROWSER_CLIENT", raising=False)


@pytest.fixture
def page() -> FakePage:
    return FakePage()


@pytest.fixture
def make_ctx(tmp_path, page: FakePage) -> Callable[..., ServerContext]:
    """Build a ServerContext wired to the fake page, with gates configurable."""

    def _make(
        *,
        require_approval: bool = True,
        headless: bool = False,
        on_site: bool = True,
        editor: bool = False,
    ) -> ServerContext:
        cfg = Config(headless=headless, require_approval=require_approval)
        cfg.data_dir = tmp_path
        cfg.__post_init__()
        # Never derive this from the environment in a test: with VEGA_DATA_DIR
        # set, captures would land in the real uploads dir.
        cfg.capture_dir = tmp_path / "captures"
        cfg.download_dir = tmp_path / "downloads"
        # A call that declared no download listens this long after it acts. Off here so
        # no other test pays for it; the ones about that window set it.
        cfg.download_settle_s = 0.0
        ctx = ServerContext(
            config=cfg,
            session=FakeSession(page),
            audit=AuditLog(cfg.audit_path),
            collab=CollaborationState(require_approval=require_approval),
        )
        # What the session does for each tab it adopts (BrowserSession._watch_downloads).
        page.on_download = ctx.downloads.on_download
        if editor:
            # A page whose rich-text body lives in a CKEditor iframe, which is
            # the shape set_editor() has to resolve on its own.
            page.frames.add("iframe.cke_wysiwyg_frame")
        if on_site:
            # Most tests act on a page the agent already reached, which is what
            # having navigated there means: NAVIGATE, and the INTERACT it implies.
            ctx.perms.grant("default", parse_origin(page.url), Capability.NAVIGATE)
        return ctx

    return _make


@pytest.fixture
def make_download(tmp_path) -> Callable[..., FakeDownload]:
    """Build a ``FakeDownload`` whose browser-side copy lives under ``tmp_path``."""

    def _make(*args: Any, **kwargs: Any) -> FakeDownload:
        return FakeDownload(tmp_path / "browser-temp", *args, **kwargs)

    return _make


@pytest.fixture
def tools_of() -> Callable[[ServerContext], Awaitable[dict[str, Callable[..., Awaitable[dict]]]]]:
    """Return ``name -> raw async tool function`` for a context.

    ``tool.fn`` is the undecorated coroutine, so tests see the plain dict a tool
    returns rather than MCP's serialised content blocks.
    """

    async def _tools(ctx: ServerContext) -> dict[str, Callable[..., Awaitable[dict]]]:
        mcp = FastMCP("test")
        register_all(mcp, ctx)
        return {tool.name: tool.fn for tool in await mcp.list_tools()}

    return _tools
