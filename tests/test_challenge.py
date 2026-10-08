"""A bot check is named, not solved: classification, the hint, and the tool wiring.

``classify`` takes plain values, so most of this runs without a browser. The tool
tests replace ``observe`` to check that a found challenge reaches the caller as a
status and a hint, and that an ordinary page reaches it untouched.
"""

from __future__ import annotations

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
