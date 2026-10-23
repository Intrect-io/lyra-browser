"""The native driver's errors, named and worded the way the tools already read them.

Nothing in lyra checks a driver error with ``isinstance``: ``actionable.describe_failure``
reads the class name (``TimeoutError``, ``TargetClosedError``), the module it comes from
(``_DRIVER_PACKAGES``) and Playwright's message wording (``Call log:``, ``element is not
visible``, ``net::ERR_ABORTED``), and ``session`` matches substrings such as ``has been
closed`` and ``is not found``. So these classes carry those names, and every message
this package builds follows Playwright's format. A wording change here is a behaviour
change for the tools; ``tests/native/test_errors.py`` pins the ones they read.
"""

from __future__ import annotations

TARGET_CLOSED = "Target page, context or browser has been closed"


class Error(Exception):
    """Base of every native driver error (Playwright's ``Error``)."""

    @property
    def message(self) -> str:
        return str(self)


class TimeoutError(Error):  # noqa: A001 — the name is the contract (see module docstring)
    """An operation ran out of time."""


class TargetClosedError(Error):
    """The page, the context or the whole browser went away under a call."""

    def __init__(self, message: str = TARGET_CLOSED) -> None:
        super().__init__(message)


class ProtocolError(Error):
    """Chrome answered a command with an error.

    Worded ``Protocol error (Method): message``, as Playwright words it: ``capture``
    retries on ``Unable to capture screenshot`` found inside that text.
    """

    def __init__(self, method: str, message: str, code: int | None = None) -> None:
        super().__init__(f"Protocol error ({method}): {message}")
        self.method = method
        self.code = code
        self.reason = message


def with_call_log(api: str, headline: str, log: list[str]) -> str:
    """``Api: headline`` followed by Playwright's ``Call log:`` block."""
    text = f"{api}: {headline}"
    if log:
        text += "\nCall log:\n" + "\n".join(f"  - {line}" for line in log)
    return text


def timeout_error(api: str, timeout_ms: float, log: list[str]) -> TimeoutError:
    return TimeoutError(with_call_log(api, f"Timeout {timeout_ms:g}ms exceeded.", log))
