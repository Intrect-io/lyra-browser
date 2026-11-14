"""A bot check is named, not solved: classification, the hint, and the tool wiring.

``classify`` takes plain values, so most of this runs without a browser. The tool
tests replace ``observe`` to check that a found challenge reaches the caller as a
status and a hint, and that an ordinary page reaches it untouched.
"""

from __future__ import annotations

import json

import pytest

from lyra_browser import challenge as ch
from lyra_browser.challenge import BLOCKED, INTERACTIVE, classify, hint_for

ORDINARY = {"url": "https://shop.example/list", "title": "Shop", "http_status": 200}


def check(**overrides):
    base = {**ORDINARY, "frame_urls": [], "body": "A normal page."}
    base.update(overrides)
    return classify(**base)


# --------------------------------------------------------------------------
# Naming the check
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame, vendor",
    [
        ("https://challenges.cloudflare.com/turnstile/v0/api.js", "cloudflare"),
        ("https://geo.captcha-delivery.com/captcha/?initialCid=x", "datadome"),
        ("https://www.google.com/recaptcha/api2/anchor?k=x", "recaptcha"),
        ("https://newassets.hcaptcha.com/captcha/v1/x/static.html", "hcaptcha"),
        ("https://client.px-cloud.net/px-captcha/index.html", "perimeterx"),
    ],
)
def test_a_known_host_in_a_frame_names_the_vendor(frame, vendor):
    found = check(frame_urls=["https://shop.example/", frame])

    assert found is not None
    assert found.vendor == vendor
    assert found.kind == INTERACTIVE


def test_cloudflare_managed_challenge_is_named_by_its_title():
    found = check(title="Just a moment...", body="")

    assert found.vendor == "cloudflare"
    assert found.kind == INTERACTIVE


def test_press_and_hold_is_perimeterx():
    found = check(body="Please Press & Hold to confirm you are a human.")

    assert found.vendor == "perimeterx"


def test_the_walmart_style_robot_question_is_named_but_not_attributed():
    found = check(title="Walmart.com", body="Robot or human?\nActivate and hold the button")

    assert found.vendor == "unidentified"
    assert found.kind == INTERACTIVE


def test_a_403_access_denied_page_is_blocked_not_interactive():
    found = check(http_status=403, title="Access Denied")

    assert found.kind == BLOCKED


def test_access_denied_without_a_403_is_not_a_block():
    # A page that merely says "access denied" in its title is not a refusal by itself.
    assert check(http_status=200, title="Access Denied") is None


def test_a_frame_from_a_known_host_wins_over_the_text():
    found = check(
        frame_urls=["https://www.google.com/recaptcha/api2/anchor"],
        body="Press & Hold",
    )

    assert found.vendor == "recaptcha"


# --------------------------------------------------------------------------
# Ordinary pages are not checks
# --------------------------------------------------------------------------


def test_an_ordinary_page_is_none():
    assert check() is None


def test_a_marker_past_the_first_3000_characters_is_ignored():
    body = "x" * 3500 + "Robot or human?"

    assert check(body=body) is None


def test_a_recaptcha_host_outside_the_frames_is_not_a_check():
    # A link to the vendor is not an embedded check.
    assert check(body="Learn about https://www.google.com/recaptcha/") is None


# --------------------------------------------------------------------------
# What the agent is told to do
# --------------------------------------------------------------------------


def test_attended_asks_the_person_to_answer_it():
    hint = hint_for(ch.Challenge("datadome", INTERACTIVE, "frame"), attended=True)

    assert "ask_user_to_do" in hint
    assert "read_page" in hint


def test_unattended_reports_blocked_and_does_not_wait():
    hint = hint_for(ch.Challenge("datadome", INTERACTIVE, "frame"), attended=False)

    assert "do not wait" in hint.lower()
    assert "outcome=blocked" in hint


def test_a_block_is_reported_the_same_way_whoever_is_watching():
    for attended in (True, False):
        hint = hint_for(ch.Challenge("unidentified", BLOCKED, "HTTP 403"), attended)
        assert "blocked" in hint.lower()
        assert "another way" in hint.lower()


# --------------------------------------------------------------------------
# observe: reading a live page, with a page that may already be gone
# --------------------------------------------------------------------------


class _Frame:
    def __init__(self, url: str) -> None:
        self.url = url


class _Page:
    def __init__(self, *, title="Shop", body="", frames=(), url="https://shop.example/"):
        self._title = title
        self._body = body
        self.frames = [_Frame(u) for u in frames]
        self.url = url

    async def title(self) -> str:
        return self._title

    async def evaluate(self, expression, arg=None):
        return self._body


class _GonePage(_Page):
    async def title(self) -> str:
        raise RuntimeError("Target closed")

    async def evaluate(self, expression, arg=None):
        raise RuntimeError("Target closed")


async def test_observe_names_a_check_on_a_live_page():
    page = _Page(frames=["https://challenges.cloudflare.com/turnstile"], body="")

    found = await ch.observe(page)

    assert found is not None and found.vendor == "cloudflare"


async def test_observe_of_an_ordinary_page_is_none():
    assert await ch.observe(_Page(body="Products")) is None


async def test_a_page_that_cannot_be_read_is_not_named_as_a_check():
    assert await ch.observe(_GonePage()) is None


# --------------------------------------------------------------------------
# Tool wiring: the status and hint reach the caller
# --------------------------------------------------------------------------


async def test_read_page_adds_the_challenge_and_keeps_the_text(make_ctx, tools_of, monkeypatch):
    from lyra_browser.tools import reading

    async def found(page, http_status=None):
        return ch.Challenge("unidentified", INTERACTIVE, "text")

    monkeypatch.setattr(reading, "observe", found)
    read = (await tools_of(make_ctx()))["read_page"]

    result = await read()

    assert result["challenge"]["vendor"] == "unidentified"
    assert "hint" in result
    assert "text" in result, "the text is still returned, so the agent can see what it is"


async def test_read_page_of_an_ordinary_page_has_no_challenge_key(make_ctx, tools_of, monkeypatch):
    from lyra_browser.tools import reading

    async def nothing(page, http_status=None):
        return None

    monkeypatch.setattr(reading, "observe", nothing)
    read = (await tools_of(make_ctx()))["read_page"]

    result = await read()

    assert "challenge" not in result
    assert "hint" not in result


# --------------------------------------------------------------------------
# navigate: the challenge branch, the same check again and again, the solver hook
# --------------------------------------------------------------------------


def _always(found):
    async def observe(page, http_status=None):
        return found

    return observe


async def test_navigate_names_the_check_and_counts_it(make_ctx, tools_of, page, monkeypatch):
    from lyra_browser.tools import navigation

    monkeypatch.setattr(
        navigation, "observe", _always(ch.Challenge("cloudflare", INTERACTIVE, "title"))
    )
    ctx = make_ctx(headless=True)
    navigate = (await tools_of(ctx))["navigate"]

    first = await navigate(url=page.url)

    assert first["status"] == "challenge" and first["challenge"]["vendor"] == "cloudflare"
    assert first["attempts"] == 1 and first["max_attempts"] == 3
    assert "nobody is at this window" in first["hint"]
    rows = [json.loads(line) for line in ctx.config.audit_path.read_text().splitlines()]
    assert any(r["tool"] == "challenge_seen" for r in rows)


async def test_the_same_check_three_times_is_abandoned(make_ctx, tools_of, page, monkeypatch):
    from lyra_browser.tools import navigation

    monkeypatch.setattr(
        navigation, "observe", _always(ch.Challenge("datadome", INTERACTIVE, "frame"))
    )
    navigate = (await tools_of(make_ctx(headless=True)))["navigate"]

    results = [await navigate(url=page.url) for _ in range(3)]

    assert [r["status"] for r in results] == ["challenge", "challenge", "challenge_abandoned"]
    assert [r["attempts"] for r in results] == [1, 2, 3]
    assert "Do not try again" in results[-1]["hint"]


async def test_the_limit_is_configurable_and_counts_per_vendor(
    make_ctx, tools_of, page, monkeypatch
):
    from lyra_browser.tools import navigation

    found = {"c": ch.Challenge("cloudflare", INTERACTIVE, "t")}

    async def observe(page, http_status=None):
        return found["c"]

    monkeypatch.setattr(navigation, "observe", observe)
    ctx = make_ctx(headless=True)
    ctx.config.challenge_max_attempts = 2
    navigate = (await tools_of(ctx))["navigate"]

    assert (await navigate(url=page.url))["status"] == "challenge"
    found["c"] = ch.Challenge("hcaptcha", INTERACTIVE, "frame")
    assert (await navigate(url=page.url))["status"] == "challenge"  # another check
    assert (await navigate(url=page.url))["status"] == "challenge_abandoned"


async def test_a_solver_that_clears_the_check_lets_the_page_through(
    make_ctx, tools_of, page, monkeypatch
):
    from lyra_browser.tools import navigation

    state = {"solved": False, "calls": []}

    async def observe(page, http_status=None):
        return None if state["solved"] else ch.Challenge("recaptcha", INTERACTIVE, "frame")

    async def solver(info, page):
        state["calls"].append(info)
        state["solved"] = True
        return "solved by the test double"

    monkeypatch.setattr(navigation, "observe", observe)
    monkeypatch.setattr(navigation, "load_solver", lambda spec: solver)
    ctx = make_ctx(headless=True)
    ctx.config.captcha_solver = "tests.double:solve"
    navigate = (await tools_of(ctx))["navigate"]

    result = await navigate(url=page.url)

    assert result["status"] == "ok" and result["challenge_solved"] == "recaptcha"
    assert result["solver"] == "solved by the test double"
    assert state["calls"][0]["vendor"] == "recaptcha" and state["calls"][0]["attempt"] == 1
    assert ctx.challenges.seen == {}, "a cleared check starts its count again"


async def test_a_solver_that_fails_leaves_the_challenge_envelope(
    make_ctx, tools_of, page, monkeypatch
):
    from lyra_browser.tools import navigation

    def solver(info, page):
        raise RuntimeError("no balance")

    monkeypatch.setattr(
        navigation, "observe", _always(ch.Challenge("recaptcha", INTERACTIVE, "frame"))
    )
    monkeypatch.setattr(navigation, "load_solver", lambda spec: solver)
    ctx = make_ctx(headless=True)
    ctx.config.captcha_solver = "tests.double:solve"
    navigate = (await tools_of(ctx))["navigate"]

    result = await navigate(url=page.url)

    assert result["status"] == "challenge"
    assert result["solver"].startswith("error: RuntimeError: no balance")


async def test_no_solver_is_called_when_someone_is_watching_or_the_page_is_blocked(
    make_ctx, tools_of, page, monkeypatch
):
    from lyra_browser.tools import navigation

    called = []
    monkeypatch.setattr(navigation, "load_solver", lambda spec: lambda i, p: called.append(i))
    monkeypatch.setattr(
        navigation, "observe", _always(ch.Challenge("recaptcha", INTERACTIVE, "frame"))
    )
    attended = make_ctx(headless=False)
    attended.config.captcha_solver = "tests.double:solve"
    assert (await (await tools_of(attended))["navigate"](url=page.url))["status"] == "challenge"

    monkeypatch.setattr(
        navigation, "observe", _always(ch.Challenge("unidentified", BLOCKED, "text"))
    )
    blocked = make_ctx(headless=True)
    blocked.config.captcha_solver = "tests.double:solve"
    assert (await (await tools_of(blocked))["navigate"](url=page.url))["status"] == "challenge"
    assert called == []


def test_load_solver_wants_module_colon_function():
    import pytest

    with pytest.raises(ValueError):
        ch.load_solver("no_colon_here")
    assert ch.load_solver("json:dumps")({"a": 1}) == '{"a": 1}'
