"""Risk gating and collaboration state for human-in-the-loop control.

Two mechanisms live here:

1. **Risk gate** — high-impact actions (navigating to a new origin, submitting a
   form, clicking a destructive control) require an explicit ``confirm=True`` when
   ``Config.require_approval`` is on. A gated call returns a structured
   ``needs_approval`` envelope instead of acting, so VEGA can ask the user first.

2. **Takeover state** — the user can grab the wheel. While a takeover is active,
   agent-driven mutation tools refuse to act and tell the model to wait.
"""

from __future__ import annotations

from dataclasses import dataclass


class ApprovalRequired(Exception):
    """Raised by gated tools when the user has not yet confirmed the action."""

    def __init__(self, action: str, reason: str, asked: bool = False) -> None:
        super().__init__(reason)
        self.action = action
        self.reason = reason
        self.asked = asked

    def envelope(self) -> dict[str, str]:
        if self.asked:
            # The client put the question to a person and this is their answer.
            # "Re-call with confirm=true" would only ask them again — on Hermes
            # that is another approval prompt in their chat — so say so.
            hint = (
                "The user was asked and did not approve. Do not re-call to get "
                "past this; tell them it was refused and retry only if they ask."
            )
        else:
            hint = "Re-call with confirm=true to proceed once the user agrees."
        return {
            "status": "needs_approval",
            "action": self.action,
            "reason": self.reason,
            "hint": hint,
        }


class TakeoverActive(Exception):
    """Raised when the user holds the session and the agent tries to mutate it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def envelope(self) -> dict[str, str]:
        return {
            "status": "takeover_active",
            "reason": self.reason,
            "hint": "The user is driving. Call resume_after_takeover when they hand back.",
        }


@dataclass(slots=True)
class CollaborationState:
    """Shared, in-process control state between the agent and the user."""

    require_approval: bool = True
    takeover: bool = False
    takeover_reason: str = ""
    user_turn: bool = False
    """Set by ``ask_user_to_do``: the user was asked to do a step in the window.

    Lighter than a takeover: the agent is not frozen (it polls with reads to see
    whether the step is done), but until it acts again the navigations the
    window makes are the user's — a sign-in, a CAPTCHA, a payment — and are not
    measured against the agent's grants. The agent's next mutating call ends it
    (``assert_agent_may_act``), as does letting go of the browser.
    """

    @property
    def user_driving(self) -> str:
        """Who the guard should credit with the window's navigations: empty when the agent.

        ``"takeover"`` or ``"ask_user_to_do"`` otherwise, which is what the audit row
        names, so a takeover and a requested step stay apart on the trail.
        """
        if self.takeover:
            return "takeover"
        if self.user_turn:
            return "ask_user_to_do"
        return ""

    def gate(self, action: str, reason: str, confirm: bool) -> None:
        """Enforce the risk gate for a high-impact action.

        Raises ApprovalRequired unless approval is disabled or confirm is True.
        """
        if not self.require_approval or confirm:
            return
        raise ApprovalRequired(action, reason)

    def assert_agent_may_act(self, *, ends_user_turn: bool = True) -> None:
        """Block agent-driven mutations while the user holds a takeover.

        Passing also takes the wheel back from a step the user was asked to do:
        from here the window's navigations are the agent's again and are judged
        against its grants. ``ends_user_turn=False`` is for the calls that only
        point at something for the user (``highlight_element``).
        """
        if self.takeover:
            raise TakeoverActive(self.takeover_reason or "User has taken over the session.")
        if ends_user_turn:
            self.user_turn = False

    def hand_to_user(self) -> None:
        """Open the user's turn (see ``user_turn``)."""
        self.user_turn = True

    def end_user_turn(self) -> None:
        """Close the user's turn without anything else (the browser was let go)."""
        self.user_turn = False
