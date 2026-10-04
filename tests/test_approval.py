import pytest

from lyra_browser.approval import ApprovalRequired, CollaborationState, TakeoverActive


def test_gate_blocks_without_confirm():
    state = CollaborationState(require_approval=True)
    with pytest.raises(ApprovalRequired) as exc:
        state.gate("navigate", "new site", confirm=False)
    env = exc.value.envelope()
    assert env["status"] == "needs_approval"
    assert env["action"] == "navigate"


def test_gate_passes_with_confirm():
    state = CollaborationState(require_approval=True)
    state.gate("navigate", "new site", confirm=True)  # no raise


def test_gate_noop_when_approval_disabled():
    state = CollaborationState(require_approval=False)
    state.gate("navigate", "new site", confirm=False)  # no raise


def test_takeover_blocks_agent():
    state = CollaborationState()
    state.takeover = True
    state.takeover_reason = "user driving"
    with pytest.raises(TakeoverActive) as exc:
        state.assert_agent_may_act()
    assert exc.value.envelope()["status"] == "takeover_active"
