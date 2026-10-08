"""Recognising a bot check, so the agent stops and hands it over instead of guessing.

A page that asks "are you a robot" is not a failure of the tool, and it is not a page
to get past. It is a page a person has to answer. What the agent can do well is say so
precisely: which vendor's check it is, what the evidence was, and whether a person is
there to answer it. Claude in Chrome and the ChatGPT agent browser do the same: a real
signed-in profile, then stop and ask the person.

``classify`` is the judgement and takes plain values, so it is tested without a
browser. ``observe`` collects those values from a page. Nothing here solves, waits out
or retries a challenge.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

INTERACTIVE = "interactive"
"""A person has to solve it (a checkbox, a puzzle, press-and-hold)."""

BLOCKED = "blocked"
"""Refused outright with no way for a person to pass it in this session."""


@dataclass(frozen=True)
class Challenge:
    vendor: str
    kind: str
    evidence: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


# Frame and script hosts that only a bot check loads.
_FRAME_HOSTS: tuple[tuple[str, str, str], ...] = (
    ("challenges.cloudflare.com", "cloudflare", INTERACTIVE),
    ("captcha-delivery.com", "datadome", INTERACTIVE),
    ("geo.captcha-delivery.com", "datadome", INTERACTIVE),
    ("google.com/recaptcha", "recaptcha", INTERACTIVE),
    ("recaptcha.net", "recaptcha", INTERACTIVE),
    ("hcaptcha.com", "hcaptcha", INTERACTIVE),
    ("px-captcha", "perimeterx", INTERACTIVE),
    ("perimeterx", "perimeterx", INTERACTIVE),
)

# Titles and visible text that name a check. Lower-cased before matching.
_TITLE_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("just a moment", "cloudflare", INTERACTIVE),
    ("attention required! | cloudflare", "cloudflare", BLOCKED),
)
_TEXT_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("press & hold", "perimeterx", INTERACTIVE),
    ("press and hold", "perimeterx", INTERACTIVE),
    ("robot or human?", "unidentified", INTERACTIVE),
    ("verify you are human", "unidentified", INTERACTIVE),
    ("checking your browser before accessing", "cloudflare", INTERACTIVE),
    ("you have been blocked", "unidentified", BLOCKED),
)
_DENIED_STATUSES = frozenset({403, 429})

_BODY_CHARS = 3000


def classify(
    *,
    url: str,
    title: str,
    http_status: int | None,
    frame_urls: list[str],
    body: str,
) -> Challenge | None:
    """The bot check this page shows, or None for an ordinary page.

    Frames are checked before text: a page that embeds a checkbox from a known host is
    named by that host, whatever its text says. Text markers are matched only in the
    first few thousand characters, so an article that quotes the phrase is not a check.
    """
    for frame in frame_urls:
        lowered = frame.lower()
        for needle, vendor, kind in _FRAME_HOSTS:
            if needle in lowered:
                return Challenge(vendor, kind, f"frame {needle} in {frame[:120]}")

    title_low = (title or "").lower()
    for needle, vendor, kind in _TITLE_MARKERS:
        if needle in title_low:
            return Challenge(vendor, kind, f"title {title!r}")

    text_low = (body or "")[:_BODY_CHARS].lower()
    for needle, vendor, kind in _TEXT_MARKERS:
        if needle in text_low:
            return Challenge(vendor, kind, f"text {needle!r} at {url[:120]}")

    if http_status in _DENIED_STATUSES and "access denied" in title_low:
        return Challenge("unidentified", BLOCKED, f"HTTP {http_status}, title {title!r}")
    return None


async def observe(page: Any, http_status: int | None = None) -> Challenge | None:
    """Read what ``classify`` needs from a live page, then classify it.

    Failures to read are not a challenge: a page that cannot be inspected is reported
    as it is by the caller, not named as a bot check.
    """
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001 — a page that is gone has no check to name
        title = ""
    try:
        body = await page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 3000) : ''"
        )
    except Exception:  # noqa: BLE001
        body = ""
    frame_urls = [f.url for f in getattr(page, "frames", []) or []]
    return classify(
        url=getattr(page, "url", "") or "",
        title=title or "",
        http_status=http_status,
        frame_urls=frame_urls,
        body=body if isinstance(body, str) else "",
    )


def hint_for(challenge: Challenge, attended: bool) -> str:
    """What the agent should do next, depending on whether a person is watching."""
    if challenge.kind == BLOCKED:
        return (
            "This page refuses automated access. Do not try another way past it; report "
            "it as blocked and continue with what remains of the task."
        )
    if attended:
        return (
            "A bot check is showing and a person can answer it. Ask them to complete it "
            "(ask_user_to_do, with the selector if there is one), then read_page again "
            "to see whether it has cleared."
        )
    return (
        "A bot check is showing and nobody is at this window to answer it. Do not try "
        "to get past it and do not wait for it: report it as blocked (finish with "
        "outcome=blocked) and continue with what remains of the task."
    )
