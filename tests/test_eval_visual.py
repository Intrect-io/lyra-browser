"""The visual eval's oracle: what counts as solving each task, without a browser.

The real-browser self-test (``scripts/eval_visual.py --brain scripted``) plays every task
right and wrong through Chrome; these pin the scoring rules and the control endpoints'
token, so a refactor of the site cannot quietly start accepting wrong answers.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from eval_visual_site import GOALS, TASKS, EvalSite, serve  # noqa: E402


def test_there_are_at_least_eight_tasks_each_with_a_goal():
    assert len(TASKS) >= 8
    assert all(GOALS[t].strip() for t in TASKS)


def _right_and_wrong(site: EvalSite, task: str) -> tuple[list, list]:
    d = site.solution(task)
    if task == "canvas_target":
        return [("click", 0)], [("click", 1)]
    if task == "icon_toolbar":
        other = next(i for i in d["icons"] if i != "trash")
        return [("click", "trash")], [("click", other)]
    if task == "toast_code":
        return [("issued", "ABC234"), ("answer", "abc234")], [("issued", "ABC234"), ("answer", "X")]
    if task == "color_state":
        return [("done", "blue")], [("done", "red")]
    if task == "canvas_modal":
        return [("closed", True), ("continue", True)], [("continue", True)]
    if task == "chart_value":
        want = d["values"][d["months"].index("Mar")]
        return [("answer", str(want))], [("answer", str(want + 1))]
    if task == "layout_fault":
        return [("report", d["faulty"])], [("report", (d["faulty"] + 1) % 4)]
    if task == "changed_panel":
        return [("applied", True), ("pick", d["changed"])], [("pick", d["changed"])]
    raise KeyError(task)


@pytest.mark.parametrize("task", TASKS)
def test_the_oracle_accepts_the_right_answer_and_rejects_a_wrong_one(task):
    site = EvalSite(seed=3)
    right, wrong = _right_and_wrong(site, task)
    for kind, value in right:
        site.record(task, kind, value)
    assert site.result(task)["success"] is True
    site.reset(task)
    right, wrong = _right_and_wrong(site, task)
    for kind, value in wrong:
        site.record(task, kind, value)
    assert site.result(task)["success"] is False


@pytest.mark.parametrize("task", TASKS)
def test_the_page_does_not_carry_the_answer_as_text(task):
    site = EvalSite(seed=5)
    html = site.page(task)
    for word in ("trash", "green star", '"faulty"', '"changed"', '"values"', '"order"'):
        assert word not in html, (task, word)


def test_a_new_round_resets_the_score():
    site = EvalSite(seed=1)
    site.record("color_state", "done", "blue")
    assert site.result("color_state")["success"]
    site.reset("color_state")
    assert site.result("color_state") == {"success": False, "events": []}
    assert site.solution("color_state")["order"][0] != "blue"


def test_control_endpoints_need_the_token():
    site = EvalSite(seed=2)
    server = serve(site)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{base}/api/chart_value/solution?token=guess", timeout=5)
        assert err.value.code == 403
        with urllib.request.urlopen(
            f"{base}/api/chart_value/solution?token={site.token}", timeout=5
        ) as r:
            assert "values" in json.loads(r.read())
        page = urllib.request.urlopen(f"{base}/task/chart_value", timeout=5).read().decode()
        assert "<canvas" in page
    finally:
        server.shutdown()
        server.server_close()


# ---- suite v2 -------------------------------------------------------------------------

from eval_visual_site import SUITES  # noqa: E402
from eval_visual_site_v2 import GOALS_V2, TASKS_V2  # noqa: E402


def _right_and_wrong_v2(site: EvalSite, task: str) -> tuple[list, list]:
    d = site.solution(task)
    if task in ("tiny_find", "tall_bar", "hover_tip"):
        n = len(d.get("codes") or d.get("heights") or d.get("tips"))
        return [("click", d["target"])], [("click", (d["target"] + 1) % n)]
    if task == "far_target":
        return [("click", 0)], [("click", 1)]
    if task == "regression":
        right = [("deployed", True), ("pick", d["changed"])]
        return right, [("pick", d["changed"])]  # picking without deploying is not seeing
    if task == "overlap":
        return [("report", d["covered"])], [("report", (d["covered"] + 1) % 4)]
    if task == "faint_text":
        return [("report", d["faint"])], [("report", (d["faint"] + 1) % 6)]
    if task == "misaligned":
        return [("click", d["off"])], [("click", (d["off"] + 1) % 36)]
    raise KeyError(task)


def test_suite_v2_has_eight_new_tasks_and_leaves_v1_alone():
    assert len(TASKS_V2) >= 8 and not set(TASKS_V2) & set(TASKS)
    assert SUITES == {"v1": TASKS, "v2": TASKS_V2}
    assert all(GOALS_V2[t].strip() for t in TASKS_V2)


@pytest.mark.parametrize("task", TASKS_V2)
def test_v2_oracle_accepts_the_right_answer_and_rejects_a_wrong_one(task):
    site = EvalSite(seed=4)
    right, _ = _right_and_wrong_v2(site, task)
    for kind, value in right:
        site.record(task, kind, value)
    assert site.result(task)["success"] is True
    site.reset(task)
    _, wrong = _right_and_wrong_v2(site, task)
    for kind, value in wrong:
        site.record(task, kind, value)
    assert site.result(task)["success"] is False


@pytest.mark.parametrize("seed", range(20))
def test_v2_rounds_have_one_answer(seed):
    site = EvalSite(seed=seed)
    tiny = site.solution("tiny_find")
    assert len(set(tiny["codes"])) == len(tiny["codes"]) == 48
    bars = site.solution("tall_bar")["heights"]
    assert bars.count(max(bars)) == 1 and sorted(bars)[-1] - sorted(bars)[-2] == 1


@pytest.mark.parametrize("task", TASKS_V2)
def test_v2_page_does_not_carry_the_answer_as_text(task):
    site = EvalSite(seed=6)
    html = site.page(task)
    d = site.solution(task)
    words = ["Archive", "hexagon", "purple", '"covered"', '"faint"', '"off"', '"changed"']
    if task == "tiny_find":
        words.append(d["codes"][d["target"]])
    if task == "far_target":
        # Its drawing code names shapes and colours (as v1's names 'green'); read_page never
        # shows a script, and the scripted self-test checks read_page itself.
        words = [w for w in words if w not in ("hexagon", "purple")]
    for word in words:
        assert word not in html, (task, word)
