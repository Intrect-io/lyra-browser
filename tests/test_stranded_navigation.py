"""A page that sends itself away while it loads must not leave a tool waiting for the driver.

The guard refuses with a 204, which commits nothing, so ``goto`` / ``go_back`` / ``reload``
keep waiting for a navigation that no longer exists until the driver's own timeout (30s).
These fakes behave that way: they count a refusal the way the route handler does, then
hang or time out. The real thing is ``scripts/verify_scriptredirect_e2e.py``.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from lyra_browser import enforcement

# (tool, the page method it drives, its arguments). On the fake page the agent is
# already on start.example, so each of them is allowed to act there.
CALLS = [
    ("navigate", "goto", {"url": "https://start.example/next", "confirm": True}),
    ("go_back", "go_back", {"confirm": True}),
    ("reload_page", "reload", {"confirm": True}),
]
EVERY_CALL = pytest.mark.parametrize(("tool", "method", "args"), CALLS)


def driver_timeout() -> Exception:
    """What the driver raises when a navigation never completes: a class of its own."""
    return type("TimeoutError", (Exception,), {})("Page.goto: Timeout 30000ms exceeded.")


def audit(ctx) -> list[dict]:
    return [json.loads(line) for line in ctx.config.audit_path.read_text().splitlines() if line]


@pytest.fixture
def short_grace(monkeypatch):
    monkeypatch.setattr(enforcement, "REFUSAL_GRACE_S", 0.1)


@EVERY_CALL
async def test_a_driver_timeout_after_a_refusal_is_answered_as_the_refusal(
    make_ctx, tools_of, page, tool, method, args
):
    ctx = make_ctx()

    async def strand(*_args, **_kwargs):
        ctx.guard.refusals += 1  # what the route handler does on a 204
        raise driver_timeout()

    setattr(page, method, strand)
    tools = await tools_of(ctx)

    result = await tools[tool](**args)

    assert result["status"] == "blocked_by_policy"
    assert result["url"] == page.url, "and says where the tab stands"
    last = audit(ctx)[-1]
    assert (last["tool"], last["status"]) == (tool, "blocked_by_policy")


@EVERY_CALL
async def test_a_timeout_nobody_caused_is_still_an_error(
    make_ctx, tools_of, page, tool, method, args
):
    """No refusal, so the site was slow or gone: the caller has to hear that as it is."""
    ctx = make_ctx()

    async def slow(*_args, **_kwargs):
        raise driver_timeout()

    setattr(page, method, slow)
    tools = await tools_of(ctx)

    with pytest.raises(Exception, match="Timeout 30000ms") as raised:
        await tools[tool](**args)

    assert type(raised.value).__name__ == "TimeoutError"


@EVERY_CALL
async def test_a_load_the_guard_stranded_is_not_waited_out(
    make_ctx, tools_of, page, short_grace, tool, method, args
):
    ctx = make_ctx()
    abandoned = asyncio.Event()

    async def hang(*_args, **_kwargs):
        ctx.guard.refusals += 1
        try:
            await asyncio.Event().wait()  # the driver's wait for a navigation that is gone
        except asyncio.CancelledError:
            abandoned.set()
            raise

    setattr(page, method, hang)
    tools = await tools_of(ctx)
    started = time.monotonic()

    # The bound is what fails a regression here, not a hung suite.
    result = await asyncio.wait_for(tools[tool](**args), timeout=5)

    assert result["status"] == "blocked_by_policy"
    assert time.monotonic() - started < 2.0, "it answered at the grace, not at the driver's timeout"
    await asyncio.wait_for(abandoned.wait(), timeout=1)  # and let go of the driver call


async def test_a_refusal_that_lets_the_load_finish_is_not_an_error(make_ctx, tools_of, page):
    """A refused form post in a frame, say: the page still loads, so the call is still ok."""
    ctx = make_ctx()

    async def refuse_then_finish(url, wait_until=None):
        ctx.guard.refusals += 1
        await asyncio.sleep(0.1)
        page.url = url

    page.goto = refuse_then_finish
    tools = await tools_of(ctx)

    result = await tools["navigate"](url="https://start.example/next", confirm=True)

    assert result["status"] == "ok"
    assert result["url"] == "https://start.example/next"


async def test_a_slow_load_is_left_alone_when_nothing_was_refused(
    make_ctx, tools_of, page, short_grace
):
    ctx = make_ctx()
    ctx.guard.refusals = 3  # refused earlier, in another call: only a new one counts

    async def slow_but_fine(url, wait_until=None):
        await asyncio.sleep(0.4)  # longer than the grace
        page.url = url

    page.goto = slow_but_fine
    tools = await tools_of(ctx)

    result = await tools["navigate"](url="https://start.example/next", confirm=True)

    assert result["status"] == "ok"
