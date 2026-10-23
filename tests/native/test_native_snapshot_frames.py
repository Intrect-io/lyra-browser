"""The snapshot writer's node rules and the frame tracker's session and offset bookkeeping.

The tree itself is compared against Playwright's in a real browser
(``testing/native_tree_parity_261023_v1.py``: 75 of 75 elements and the same in-view
order, refs acted on in a same-site and a cross-site iframe). These pin the parts that
decide what that comparison sees.
"""

from __future__ import annotations

import asyncio

from lyra_browser.native.frames import FrameRef, FrameTracker
from lyra_browser.native.snapshot import _ROLE_MAP, _states
from lyra_browser.tools.reading import compact_snapshot


def _node(**props) -> dict:
    return {"properties": [{"name": k, "value": {"value": v}} for k, v in props.items()]}


def test_states_print_as_playwright_prints_them():
    assert _states(_node(checked="true", level=1, disabled=False)) == ["checked", "level=1"]
    assert _states(_node(checked="mixed")) == ["checked=mixed"]
    assert _states(_node(expanded="false", pressed=True)) == ["pressed"]


def test_chromes_internal_roles_map_to_what_the_reader_lists():
    assert _ROLE_MAP["MenuListOption"] == "option"
    assert _ROLE_MAP["StaticText"] == "text"
    assert _ROLE_MAP["image"] == "img"
    assert _ROLE_MAP["RootWebArea"] is None and _ROLE_MAP["InlineTextBox"] is None


def test_the_written_format_reads_back_through_compact_snapshot():
    raw = "\n".join(
        [
            '- heading "Title" [level=1] [ref=e1] [box=8,8,200,30]',
            '- link "Home" [ref=e2] [box=8,40,40,20]',
            "  - /url: /home",
            '- button "Say \\"hi\\": now" [disabled] [ref=e3] [box=8,70,80,22]',
            "- iframe [ref=e4] [box=300,0,400,100]",
            '  - button "Inner" [ref=f1e1] [box=8,8,60,22]',
            '- button "Far" [ref=e5] [box=8,2000,60,22]',
        ]
    )
    tree = compact_snapshot(raw, refs=True, viewport=(1280, 800))
    lines = tree.text.splitlines()
    assert lines[:4] == [
        'aria-ref=e1 heading "Title" [level=1]',
        'aria-ref=e2 link "Home" -> /home',
        'aria-ref=e3 button "Say \\"hi\\": now" [disabled]',
        'aria-ref=f1e1 button "Inner"',
    ]
    assert tree.in_viewport == 4 and lines[-1] == 'aria-ref=e5 button "Far"'


class _FakePage:
    """What FrameTracker asks of a page: its session, its frames, and box answers."""

    _session = "PAGE"
    main_frame_id = "MAIN"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []

        class Worlds:
            def forget(self, _frame_id):
                pass

        self._worlds = Worlds()

    def owns_frame(self, frame_id: str) -> bool:
        return frame_id in {"MAIN", "SAME"}

    async def _send_on(self, session: str, method: str, params: dict | None = None, **_kw) -> dict:
        self.sent.append((session, method, params or {}))
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": {"OOPIF": 7, "NESTED": 9}[params["frameId"]]}
        if method == "DOM.getBoxModel":
            offsets = {7: (100, 50), 9: (10, 5)}[params["backendNodeId"]]
            x, y = offsets
            return {"model": {"content": [x, y, x + 300, y, x + 300, y + 200, x, y + 200]}}
        return {}


def test_frames_route_to_their_session_and_offsets_add_up_across_processes(monkeypatch):
    page = _FakePage()
    tracker = FrameTracker(page)  # type: ignore[arg-type]
    monkeypatch.setattr(asyncio, "ensure_future", lambda coro: (coro.close(), _done())[1])
    tracker._on_attached(
        {"targetInfo": {"type": "iframe", "targetId": "OOPIF"}, "sessionId": "S1"}, "PAGE"
    )
    tracker._on_attached(
        {"targetInfo": {"type": "iframe", "targetId": "NESTED"}, "sessionId": "S2"}, "S1"
    )

    assert tracker.frame("MAIN") == FrameRef("MAIN", "PAGE")
    assert tracker.frame("SAME") == FrameRef("SAME", "PAGE")
    assert tracker.frame("NESTED") == FrameRef("NESTED", "S2")
    assert tracker.is_out_of_process("OOPIF") and not tracker.is_out_of_process("SAME")

    offset = asyncio.run(tracker.viewport_offset(tracker.frame("NESTED")))
    assert offset == (110.0, 55.0)
    assert asyncio.run(tracker.viewport_offset(tracker.frame("SAME"))) == (0.0, 0.0)

    tracker._on_detached({"sessionId": "S2"})
    assert tracker.frame("NESTED") == FrameRef("NESTED", "PAGE")
    assert not tracker.knows("NESTED")


def _done():
    class Done:
        def add_done_callback(self, _cb):
            pass

    return Done()
