"""Selector parsing, key combos and call-log bookkeeping of the native driver.

What a real browser does with them is covered by
``testing/native_locator_smoke_261023_v1.py``; these pin the parsing the browser never
sees, where a mistake turns one selector into another without any error.
"""

from __future__ import annotations

import pytest

from lyra_browser.native.errors import Error
from lyra_browser.native.input import LAYOUT, split_combo
from lyra_browser.native.locator import _note, _states_match
from lyra_browser.native.selectors import parse, text_match


@pytest.mark.parametrize(
    ("selector", "engines"),
    [
        ("button.save", ["css"]),
        ("button.save:visible", ["css", "visible"]),
        ("div:has-text('a >> b') >> span", ["css", "has-text", "css"]),
        ("text=Log in", ["text"]),
        ('"Log in"', ["text"]),
        ("//div[@id='x']", ["xpath"]),
        ("xpath=//a", ["xpath"]),
        ("#list li >> nth=-1", ["css", "nth"]),
        ("aria-ref=f1e3", ["aria-ref"]),
        ('role=button[name="Save"]', ["role"]),
        ("input[name=q]", ["css"]),
        ("id=main", ["css"]),
        ("data-testid=go", ["css"]),
    ],
)
def test_parts_and_engines(selector, engines):
    assert [p.engine for p in parse(selector)] == engines


def test_text_matching_follows_playwright():
    assert text_match("Log in").matches("  please LOG   in now ")
    assert text_match('"Log in"').matches("Log in")
    assert not text_match('"Log in"').matches("Log in now")
    assert text_match("/log\\s*in/i").matches("LOGIN")


def test_role_names_are_case_insensitive_unless_marked_exact():
    (loose,) = parse('role=button[name="save"]')
    (strict,) = parse('role=button[name="Save"s][level=2]')
    assert loose.name.matches("Save draft")
    assert strict.name.matches("Save") and not strict.name.matches("save")
    assert strict.states == (("level", "2"),)


@pytest.mark.parametrize("bad", ["", "   ", "label=Email", "nth=x", "a >> >> b"])
def test_unreadable_selectors_are_refused_not_guessed(bad):
    with pytest.raises(Error):
        parse(bad)


def test_id_values_are_escaped_into_css():
    (part,) = parse('id=a"b')
    assert part.body == '[id="a\\"b"]'


@pytest.mark.parametrize(
    ("combo", "mods", "key"),
    [
        ("Enter", [], "Enter"),
        ("Control+a", ["Control"], "a"),
        ("Control+Shift+Z", ["Control", "Shift"], "Z"),
        ("+", [], "+"),
        ("Shift++", ["Shift"], "+"),
    ],
)
def test_key_combos(combo, mods, key):
    assert split_combo(combo) == (mods, key)


def test_unknown_modifiers_are_refused():
    with pytest.raises(Error):
        split_combo("Hyper+a")


def test_layout_covers_what_tools_press():
    for key in ("Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowDown", "a", "A", "1", " "):
        assert key in LAYOUT
    assert LAYOUT["Enter"].text == "\r"


def test_call_log_keeps_its_head_drops_repeats_and_caps_its_length():
    log = ["waiting for locator('x')"]
    _note(log, "  - element is not visible")
    _note(log, "  - element is not visible")
    _note(log, "  - locator resolved to <a>", once=True)
    _note(log, "  - element is not visible")
    _note(log, "  - locator resolved to <a>", once=True)
    assert log == [
        "waiting for locator('x')",
        "  - element is not visible",
        "  - locator resolved to <a>",
        "  - element is not visible",
    ]
    for i in range(100):
        _note(log, f"line {i}")
    assert log[0] == "waiting for locator('x')" and len(log) <= 41 and log[-1] == "line 99"


def test_role_states_compare_as_chrome_reports_them():
    node = {
        "properties": [
            {"name": "checked", "value": {"value": "true"}},
            {"name": "level", "value": {"value": 2}},
        ]
    }
    assert _states_match(node, (("checked", "true"), ("level", "2")))
    assert not _states_match(node, (("disabled", "true"),))
    assert _states_match(node, (("disabled", "false"),))
