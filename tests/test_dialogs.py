"""A native dialog is answered the moment a page raises it, reported once, and
answered the way the agent chose.

A page that raises ``alert()``/``confirm()``/``prompt()`` stops until somebody
answers, and once a listener exists the driver leaves the answering to it. The
first version of the forms tools installed a listener that never awaited the
driver's answer, which froze the page on the first refused save. So these tests
read what each fake dialog was *asked to do* — and how often — not only what the
tools reported.
"""

from __future__ import annotations

import asyncio
import gc
import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from conftest import FakeDialog, FakePage
from lyra_browser.config import Config
from lyra_browser.dialogs import BUFFER_SIZE, MESSAGE_CHARS, DialogHandler
from lyra_browser.session import BrowserSession
from lyra_browser.tools import forms


class AsyncDialog(FakeDialog):
    """A driver's dialog: answering is a coroutine, and nothing happens until it runs."""

    async def dismiss(self) -> None:
        super().dismiss()

    async def accept(self, prompt_text: str | None = None) -> None:
        super().accept(prompt_text)


class AlreadyHandled(AsyncDialog):
    """A dialog somebody else answered first — the driver refuses a second answer."""

    async def dismiss(self) -> None:
        raise RuntimeError("Dialog.dismiss: Cannot dismiss dialog which is already handled!")

    async def accept(self, prompt_text: str | None = None) -> None:
        raise RuntimeError("Dialog.accept: Cannot accept dialog which is already handled!")


class UnreadableDialog:
    """A dialog whose text the driver cannot produce."""

    type = "confirm"
    default_value = ""
    page = None

    def __init__(self) -> None:
        self.answers: list[tuple] = []

    @property
    def message(self) -> str:
        raise RuntimeError("the driver could not read the dialog")

    def dismiss(self) -> None:
        self.answers.append(("dismiss",))

    def accept(self, prompt_text: str | None = None) -> None:
        self.answers.append(("accept", prompt_text))


def raise_on(handler: DialogHandler, dialog):
    """Deliver ``dialog`` the way the context's listener receives it."""
    handler.handle(dialog)
    return dialog


# --------------------------------------------------------------------------
# The default answer: what a browser with no listener would have done
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "default_value", "answer", "handled"),
    [
        ("alert", "", ("accept", None), "accepted"),
        ("beforeunload", "", ("accept", None), "accepted"),
        ("confirm", "", ("dismiss",), "dismissed"),
        ("prompt", "Ada", ("dismiss",), "dismissed"),
    ],
)
def test_an_unarmed_dialog_gets_what_a_browser_without_a_listener_gives(
    kind, default_value, answer, handled
):
    """A page must not see anything new: confirm() stays false, prompt() null, and a
    navigation is not cancelled by its own beforeunload."""
    handler = DialogHandler()

    dialog = raise_on(handler, FakeDialog("Sure?", type=kind, default_value=default_value))

    assert dialog.answers == [answer]
    assert handler.drain() == [
        {
            "type": kind,
            "message": "Sure?",
            "default_value": default_value,
            "handled": handled,
            "armed": False,
        }
    ]


def test_a_dialog_whose_report_cannot_be_written_is_still_answered():
    """A listener that raises before answering leaves the page frozen."""
    handler = DialogHandler()

    unreadable = raise_on(handler, UnreadableDialog())
    after = raise_on(handler, FakeDialog("next", type="confirm"))

    assert unreadable.answers == [("dismiss",)]
    assert after.answers == [("dismiss",)], "and one bad dialog does not end the handler"
    assert [d["message"] for d in handler.drain()] == ["next"]


async def test_the_drivers_coroutine_answer_is_awaited_not_just_created():
    """``dialog.dismiss()`` on a real page returns a coroutine. Calling it without
    awaiting it — as the forms tools once did — answers nothing and freezes the page."""
    handler = DialogHandler()

    dialog = raise_on(handler, AsyncDialog("Delete it?", type="confirm"))
    await asyncio.sleep(0)

    assert dialog.answers == [("dismiss",)]


async def test_an_answer_the_driver_refuses_is_not_the_handlers_problem():
    """The person at a headful window can answer first; ours then fails as "already
    handled". That must neither leak into the event loop nor stop later dialogs."""
    problems: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: problems.append(ctx))
    handler = DialogHandler()

    raise_on(handler, AlreadyHandled("late", type="confirm"))
    fine = raise_on(handler, AsyncDialog("next", type="confirm"))
    for _ in range(3):
        await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)

    assert problems == []
    assert fine.answers == [("dismiss",)]
    assert [d["message"] for d in handler.drain()] == ["late", "next"]


# --------------------------------------------------------------------------
# Armed answers: for the next call's action, once, and no longer
# --------------------------------------------------------------------------


def arm(handler: DialogHandler, accept: bool = True, text: str = "") -> None:
    """What ``handle_dialog`` does: a call begins, then it arms."""
    handler.begin_call()
    handler.arm(accept, text)


@contextmanager
def next_call(handler: DialogHandler):
    """The call after the arming one, while its action is under way."""
    handler.begin_call()
    with handler.acting():
        yield


def test_an_armed_answer_is_spent_by_the_first_dialog():
    handler = DialogHandler()
    arm(handler)

    with next_call(handler):
        first = raise_on(handler, FakeDialog("Delete it?", type="confirm"))
        second = raise_on(handler, FakeDialog("Really?", type="confirm"))

    assert first.answers == [("accept", None)]
    assert second.answers == [("dismiss",)], "the yes was for one dialog"
    assert [d["armed"] for d in handler.drain()] == [True, False]


@pytest.mark.parametrize("kind", ["alert", "beforeunload"])
def test_an_armed_no_beats_a_default_yes(kind):
    """Dismissing a beforeunload keeps the page where it is."""
    handler = DialogHandler()
    arm(handler, accept=False, text="ignored when dismissing")

    with next_call(handler):
        dialog = raise_on(handler, FakeDialog("Leave?", type=kind))

    assert dialog.answers == [("dismiss",)]
    assert handler.drain()[0]["handled"] == "dismissed"


def test_armed_text_reaches_the_prompt_and_is_never_reported():
    handler = DialogHandler()
    arm(handler, text="Ada Lovelace")

    with next_call(handler):
        dialog = raise_on(handler, FakeDialog("Name?", type="prompt", default_value="anon"))

    assert dialog.answers == [("accept", "Ada Lovelace")]
    report = handler.drain()
    assert "Ada Lovelace" not in json.dumps(report), "what a prompt is told may be a secret"
    assert report[0]["handled"] == "accepted" and report[0]["armed"] is True


def test_accepting_a_prompt_without_text_sends_the_default_like_the_ok_button():
    """The driver's own accept() with no text answers "" (measured in Chrome), so the
    handler passes the box's content itself."""
    handler = DialogHandler()
    arm(handler)

    with next_call(handler):
        dialog = raise_on(handler, FakeDialog("Name?", type="prompt", default_value="anon"))

    assert dialog.answers == [("accept", "anon")]


@pytest.mark.parametrize(("waited_s", "applies"), [(29.0, True), (31.0, False)])
def test_an_armed_answer_still_lapses_with_time(waited_s, applies):
    """The backstop: a call that starts long after the arming is not the call it was for."""
    now = [500.0]
    handler = DialogHandler(ttl_s=30.0, clock=lambda: now[0])
    arm(handler)
    now[0] += waited_s

    with next_call(handler):
        dialog = raise_on(handler, FakeDialog("Delete it?", type="confirm"))

    assert dialog.answers == [("accept", None) if applies else ("dismiss",)]
    assert handler.drain()[0]["armed"] is applies


def test_arming_again_replaces_an_answer_nobody_used():
    handler = DialogHandler()
    arm(handler, accept=True)
    arm(handler, accept=False)

    with next_call(handler):
        dialog = raise_on(handler, FakeDialog("Sure?", type="confirm"))

    assert dialog.answers == [("dismiss",)]
    assert handler.drain()[0]["armed"] is True, "the second arming answered it, not the default"


# A yes armed for one click must not answer the "Delete this?" of a page the agent
# reaches later: the answer is bound to the next call, like a one-shot grant.


def test_a_dialog_outside_the_action_does_not_spend_the_answer_meant_for_it():
    handler = DialogHandler()
    arm(handler)

    between = raise_on(handler, FakeDialog("a timer, between calls", type="confirm"))
    handler.begin_call()
    checking = raise_on(handler, FakeDialog("a timer, while the call checks", type="confirm"))
    with handler.acting():
        meant = raise_on(handler, FakeDialog("Delete it?", type="confirm"))

    assert between.answers == [("dismiss",)] and checking.answers == [("dismiss",)]
    assert meant.answers == [("accept", None)], "the action's own dialog still gets the yes"


def test_a_call_that_began_before_the_arming_one_does_not_inherit_the_answer():
    """A click still checking its target when handle_dialog runs is not the next call."""
    handler = DialogHandler()
    handler.begin_call()  # the click
    arm(handler)  # handle_dialog, meanwhile

    with handler.acting():  # the click starts acting only now
        early = raise_on(handler, FakeDialog("Delete it?", type="confirm"))
    with next_call(handler):  # the call that follows the arming one
        meant = raise_on(handler, FakeDialog("Delete it?", type="confirm"))

    assert early.answers == [("dismiss",)]
    assert meant.answers == [("accept", None)]


def test_the_answer_is_gone_once_the_call_it_was_armed_for_has_finished():
    """A slow earlier call that starts acting late must not pick up what the next call left."""
    handler = DialogHandler()
    handler.begin_call()  # call 1: still checking its target
    arm(handler)  # call 2: handle_dialog
    with next_call(handler):  # call 3: the call it was armed for; nothing was raised
        pass

    with handler.acting():  # call 1 gets to its action at last
        late = raise_on(handler, FakeDialog("Delete it?", type="confirm"))

    assert late.answers == [("dismiss",)]


# --------------------------------------------------------------------------
# The report: bounded, drained once, cut where the page can write without limit
# --------------------------------------------------------------------------


def test_the_report_keeps_the_newest_dialogs_and_forgets_what_was_reported():
    handler = DialogHandler()
    for n in range(BUFFER_SIZE + 7):
        raise_on(handler, FakeDialog(f"m{n}"))

    kept = handler.drain()

    assert [d["message"] for d in kept] == [f"m{n}" for n in range(7, BUFFER_SIZE + 7)]
    assert handler.drain() == [], "a dialog is reported once"


def test_a_long_message_is_cut_in_the_report_but_whole_for_a_watching_tool():
    handler = DialogHandler()
    page = FakePage()
    text = "Fix these: " + "x" * 800

    raise_on(handler, FakeDialog(text, page=page))
    with handler.watch(page) as said:
        raise_on(handler, FakeDialog(text, page=page))

    assert handler.drain()[0]["message"] == text[:MESSAGE_CHARS]
    assert said == [text], "a refusal the tool reports must not lose its reasons"


# --------------------------------------------------------------------------
# A tool watching its page for a verdict
# --------------------------------------------------------------------------


def test_a_watch_takes_the_messages_of_its_own_page_and_nobody_elses():
    handler = DialogHandler()
    page, other = FakePage(), FakePage()

    with handler.watch(page) as said:
        first = raise_on(handler, FakeDialog("  Title required \n", page=page))
        raise_on(handler, FakeDialog("elsewhere", page=other))
        second = raise_on(handler, FakeDialog("Body too short", type="confirm", page=page))
    raise_on(handler, FakeDialog("after", page=page))

    assert said == ["Title required", "Body too short"]
    assert first.answers and second.answers, "a watch does not answer instead of the handler"
    assert [d["message"] for d in handler.drain()] == ["elsewhere", "after"]


# --------------------------------------------------------------------------
# The session listens on the whole window, and takes its state with it
# --------------------------------------------------------------------------


class FakeTab:
    url = "about:blank"


class FakeBrowser:
    """A persistent context that opens with one blank tab."""

    def __init__(self) -> None:
        self.pages = [FakeTab()]
        self.listeners: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    async def route(self, pattern, handler) -> None:
        pass

    async def new_page(self) -> FakeTab:
        return self.open(FakeTab())

    def open(self, tab: FakeTab) -> FakeTab:
        """A popup or a target=_blank link: the page joins ``pages``, then "page" fires."""
        self.pages.append(tab)
        for handler in self.listeners.get("page", []):
            handler(tab)
        return tab

    def raise_dialog(self, dialog: FakeDialog) -> FakeDialog:
        for handler in list(self.listeners.get("dialog", [])):
            handler(dialog)
        return dialog

    async def close(self) -> None:
        self.pages = []


class FakePlaywright:
    def __init__(self) -> None:
        self.browsers: list[FakeBrowser] = []
        self.chromium = self

    async def launch_persistent_context(self, **kwargs) -> FakeBrowser:
        self.browsers.append(FakeBrowser())
        return self.browsers[-1]

    async def stop(self) -> None:
        pass


@pytest.fixture
def session_over_fake(tmp_path, monkeypatch):
    cfg = Config(headless=False)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    cfg.capture_dir = tmp_path / "captures"
    session = BrowserSession(cfg)
    pw = FakePlaywright()

    async def start_playwright():
        return pw

    monkeypatch.setattr(session, "_start_playwright", start_playwright)
    return session, pw


async def test_a_popup_that_asks_while_it_loads_is_answered_and_reported(session_over_fake):
    """Measured on Chrome: a listener added to a tab after its "page" event never
    sees the dialog its first script raises, so the listener has to be the window's."""
    session, pw = session_over_fake
    await session.start()
    browser = pw.browsers[-1]
    popup = FakeTab()

    on_opener = browser.raise_dialog(
        FakeDialog("on the opener", type="confirm", page=browser.pages[0])
    )
    at_load = browser.raise_dialog(FakeDialog("at load", type="confirm", page=popup))
    browser.open(popup)  # only now does the session hear about the tab
    later = browser.raise_dialog(FakeDialog("later", type="confirm", page=popup))

    assert [d.answers for d in (on_opener, at_load, later)] == [[("dismiss",)]] * 3
    assert [d["message"] for d in session.dialogs.drain()] == ["on the opener", "at load", "later"]


async def test_closing_the_window_takes_its_dialogs_and_armed_answer_with_it(session_over_fake):
    session, pw = session_over_fake
    await session.start()
    pw.browsers[-1].raise_dialog(FakeDialog("from the old window"))
    arm(session.dialogs)

    await session.stop()
    await session.start()
    with next_call(session.dialogs):  # the first call on the new window
        dialog = pw.browsers[-1].raise_dialog(FakeDialog("Sure?", type="confirm"))

    assert dialog.answers == [("dismiss",)], "the next window inherits no answer"
    assert [d["message"] for d in session.dialogs.drain()] == ["Sure?"]


# --------------------------------------------------------------------------
# Through the tools
# --------------------------------------------------------------------------


def audit(ctx) -> list[dict]:
    return [json.loads(line) for line in Path(ctx.config.audit_path).read_text().splitlines()]


@pytest.fixture
def ctx(make_ctx):
    return make_ctx()


CONFIRM = {
    "type": "confirm",
    "message": "Delete the item?",
    "default_value": "",
}


async def test_an_armed_yes_answers_the_confirm_a_click_raises(ctx, tools_of, page):
    tools = await tools_of(ctx)
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]

    armed = await tools["handle_dialog"](accept=True)
    clicked = await tools["click"](selector="#delete")

    assert armed["status"] == "ok" and armed["accept"] is True
    assert page.raised_dialogs[0].answers == [("accept", None)]
    assert clicked["dialogs"] == [{**CONFIRM, "handled": "accepted", "armed": True}]


async def test_without_arming_the_confirm_is_refused_and_the_reply_says_so(ctx, tools_of, page):
    tools = await tools_of(ctx)
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]

    clicked = await tools["click"](selector="#delete")

    assert page.raised_dialogs[0].answers == [("dismiss",)]
    assert clicked["status"] == "ok"
    assert clicked["dialogs"] == [{**CONFIRM, "handled": "dismissed", "armed": False}]


async def test_the_prompt_text_reaches_the_page_and_no_record_of_ours(ctx, tools_of, page):
    tools = await tools_of(ctx)
    page.dialogs_on_click = [FakeDialog("Your name?", type="prompt", default_value="anon")]

    await tools["handle_dialog"](accept=True, text="hunter2")
    clicked = await tools["click"](selector="#ask")

    assert page.raised_dialogs[0].answers == [("accept", "hunter2")]
    assert "hunter2" not in json.dumps(clicked)
    assert "hunter2" not in Path(ctx.config.audit_path).read_text(), "audit: length, not text"
    entry = next(e for e in audit(ctx) if e["tool"] == "handle_dialog")
    assert entry["args"] == {"accept": True, "text_chars": 7}


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("click", {"selector": "#go"}),
        ("type_text", {"selector": "#q", "value": "hello"}),
        ("press_key", {"key": "Enter"}),
    ],
)
async def test_a_dialog_raised_between_calls_is_reported_once_by_the_next_action(
    ctx, tools_of, page, tool, kwargs
):
    """setTimeout(alert) fires while nobody is calling; the page must not have
    hung, and the next action says it happened."""
    tools = await tools_of(ctx)
    page.raise_dialog("Your session expires in 1 minute")

    result = await tools[tool](**kwargs)
    again = await tools[tool](**kwargs)

    assert result["dialogs"] == [
        {
            "type": "alert",
            "message": "Your session expires in 1 minute",
            "default_value": "",
            "handled": "accepted",
            "armed": False,
        }
    ]
    assert "dialogs" not in again, "only when there is something to report, and only once"
    assert page.raised_dialogs[0].answers == [("accept", None)]


async def test_a_reply_without_dialogs_has_no_dialogs_key(ctx, tools_of):
    tools = await tools_of(ctx)

    assert "dialogs" not in await tools["click"](selector="#go")


async def test_an_armed_yes_does_not_reach_a_confirm_two_calls_later(ctx, tools_of, page):
    """The leak this closes: a yes armed for one click answering the 'Delete this?' of
    whatever page the agent reaches afterwards."""
    tools = await tools_of(ctx)
    await tools["handle_dialog"](accept=True)
    first = await tools["click"](selector="#plain")  # the call it was armed for; no dialog
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]

    second = await tools["click"](selector="#delete")

    assert first["status"] == "ok" and "dialogs" not in first
    assert page.raised_dialogs[0].answers == [("dismiss",)]
    assert second["dialogs"] == [{**CONFIRM, "handled": "dismissed", "armed": False}]


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("navigate", {"url": "https://start.example/next"}),
        ("type_text", {"selector": "#field", "value": "text"}),
        ("press_key", {"key": "Escape"}),
        ("hover", {"selector": "#menu"}),
        ("select_option", {"selector": "#plain", "value": "1"}),
    ],
)
async def test_whichever_tool_acts_next_uses_the_armed_answer_up(ctx, tools_of, page, tool, kwargs):
    tools = await tools_of(ctx)
    await tools["handle_dialog"](accept=True)
    used = await tools[tool](**kwargs)
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]

    later = await tools["click"](selector="#delete")

    assert used["status"] == "ok"
    assert later["dialogs"] == [{**CONFIRM, "handled": "dismissed", "armed": False}]


@pytest.mark.parametrize("tool", ["get_url", "read_page", "read_form", "read_draft", "screenshot"])
async def test_reading_between_the_arming_and_the_action_leaves_the_answer_alone(
    ctx, tools_of, page, tool
):
    """Reading is not driving the browser: it neither spends the answer nor ends it."""
    tools = await tools_of(ctx)
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]
    await tools["handle_dialog"](accept=True)

    await tools[tool]()
    clicked = await tools["click"](selector="#delete")

    assert clicked["dialogs"] == [{**CONFIRM, "handled": "accepted", "armed": True}]


async def test_a_refused_click_uses_the_armed_answer_up(ctx, tools_of, page):
    """The refused call was the next call: the retry after an approval is armed again."""
    tools = await tools_of(ctx)
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]
    await tools["handle_dialog"](accept=True)

    refused = await tools["click"](selector="#delete", submits=True)
    retried = await tools["click"](selector="#delete", submits=True, confirm=True)

    assert refused["status"] == "needs_approval"
    assert retried["dialogs"] == [{**CONFIRM, "handled": "dismissed", "armed": False}]


async def test_a_dialog_raised_after_the_armed_call_returned_gets_the_default(ctx, tools_of, page):
    """A page's own timer, firing after the click it was armed for has returned."""
    tools = await tools_of(ctx)
    await tools["handle_dialog"](accept=True)
    await tools["click"](selector="#schedule")  # raises nothing itself
    late = page.raise_dialog(FakeDialog("Still there?", type="confirm"))

    reported = await tools["press_key"](key="Shift")

    assert late.answers == [("dismiss",)]
    assert reported["dialogs"] == [
        {
            "type": "confirm",
            "message": "Still there?",
            "default_value": "",
            "handled": "dismissed",
            "armed": False,
        }
    ]


async def test_a_stray_dialog_before_the_action_does_not_spend_the_answer_meant_for_it(
    ctx, tools_of, page
):
    tools = await tools_of(ctx)
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]
    await tools["handle_dialog"](accept=True)
    stray = page.raise_dialog(FakeDialog("Session expiring", type="confirm"))  # a page timer

    clicked = await tools["click"](selector="#delete")

    assert stray.answers == [("dismiss",)]
    assert [(d["message"], d["armed"]) for d in clicked["dialogs"]] == [
        ("Session expiring", False),
        (CONFIRM["message"], True),
    ]
    assert page.raised_dialogs[1].answers == [("accept", None)]


async def test_another_session_cannot_arm_the_holders_dialogs(ctx, tools_of, page):
    tools = await tools_of(ctx)
    page.dialogs_on_click = [FakeDialog(CONFIRM["message"], type="confirm")]
    ctx.owner_session = "someone-else"
    ctx.owner_seen_at = time.monotonic()

    refused = await tools["handle_dialog"](accept=True)
    ctx.owner_session = None  # the holder let go; this session drives now
    clicked = await tools["click"](selector="#delete")

    assert refused["status"] == "session_conflict"
    assert clicked["dialogs"] == [{**CONFIRM, "handled": "dismissed", "armed": False}]


# --------------------------------------------------------------------------
# save_draft / publish keep reporting a refusal that arrives as a dialog
# --------------------------------------------------------------------------


@pytest.fixture
def instant_settle(monkeypatch):
    """The fake page cannot wait, so the tools fall back to a real 3s sleep."""
    monkeypatch.setattr(forms, "_SETTLE_BUDGET_S", 0.0)


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("save_draft", {"submit": "#send"}),
        ("publish", {"selector": "#live", "submit": "#send"}),
    ],
)
async def test_a_form_refused_in_a_dialog_is_reported_refused_and_only_once(
    ctx, tools_of, page, instant_settle, tool, kwargs
):
    tools = await tools_of(ctx)
    page.dialogs_on_click = ["The form appeared to be incomplete"]

    sent = await tools[tool](confirm=True, **kwargs)
    next_reply = await tools["click"](selector="#go")

    assert sent["status"] == "refused" and sent["refused"] is True
    assert sent["errors"] == ["The form appeared to be incomplete"]
    assert page.raised_dialogs[0].answers, "and the dialog was answered, not left open"
    assert "dialogs" not in next_reply, "the same refusal is not reported a second time"


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("save_draft", {"submit": "#send"}),
        ("publish", {"selector": "#live", "submit": "#send"}),
    ],
)
async def test_a_form_that_raises_no_dialog_is_not_refused(
    ctx, tools_of, page, instant_settle, tool, kwargs
):
    sent = await (await tools_of(ctx))[tool](confirm=True, **kwargs)

    assert sent["status"] == "ok" and sent["errors"] == []
    assert "refused" not in sent or sent["refused"] is False


async def test_the_form_tools_await_the_drivers_answer_to_the_refusing_dialog(
    ctx, tools_of, page, instant_settle
):
    """The original hang: ``dialog.dismiss()`` called and never awaited."""
    page.dialogs_on_click = [AsyncDialog("The form appeared to be incomplete")]

    sent = await (await tools_of(ctx))["save_draft"](submit="#send", confirm=True)
    await asyncio.sleep(0)

    assert sent["status"] == "refused"
    assert page.raised_dialogs[0].answers == [("accept", None)]
