"""Filling a real form: reading it, choosing options, writing rich text, uploading.

Each test names something ``click``/``type_text`` could not do, measured on KVR's
developer forms — a ``<select>`` is not a click target, a CKEditor body is an
iframe rather than the hidden textarea the form posts, an image picker keeps its
``<input type=file>`` hidden, and ``read_page``''s ``innerText`` carries no field
names or ``<select>`` options at all.

Gates get both halves asserted: the envelope *and* an untouched page, because a
gate that returns ``needs_approval`` after filling a field is not a gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeLocator
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability

SITE = "https://start.example/"


def _audit(ctx) -> list[dict]:
    return [json.loads(line) for line in Path(ctx.config.audit_path).read_text().splitlines()]


def _calls(page, name: str) -> list[tuple]:
    return [call for call in page.calls if call[0] == name]


def _read_form_scope(page) -> dict:
    """The argument object read_form() handed the page's evaluate()."""
    for call in page.calls:
        if call[0] == "evaluate" and len(call) > 2 and isinstance(call[2], dict):
            return call[2]
    raise AssertionError("read_form never called page.evaluate with an argument")


# --------------------------------------------------------------------------
# read_form: the names, types and options read_page loses
# --------------------------------------------------------------------------


async def test_read_form_returns_fields_with_names_and_options(make_ctx, tools_of):
    tools = await tools_of(make_ctx())
    result = await tools["read_form"]()

    assert result["status"] == "ok"
    names = [f["name"] for f in result["fields"]]
    assert "inst" in names and "copy_prot" in names
    select = next(f for f in result["fields"] if f["name"] == "copy_prot")
    assert [o["value"] for o in select["options"]] == ["0", "9"]
    # Hidden inputs are counted, not dumped — a 188-field form is mostly secrets.
    assert result["hiddenCount"] == 3
    assert result["buttons"][0]["text"] == "Submit"


async def test_read_form_reports_the_editor_it_will_need(make_ctx, tools_of):
    """The iframe is the thing to address, and read_form says so."""
    result = await (await tools_of(make_ctx()))["read_form"]()

    assert result["editors"][0]["cls"].startswith("cke_wysiwyg_frame")


async def test_read_form_reports_what_the_editor_currently_holds(make_ctx, tools_of):
    """A write has to be checkable afterwards.

    ``read_page`` cannot do it: the editor body is a separate document, so its
    text never appears in the main frame's ``innerText``.
    """
    result = await (await tools_of(make_ctx()))["read_form"]()

    editor = result["editors"][0]
    assert editor["instance"] == "news"
    assert "content" in editor


async def test_read_form_does_not_claim_the_session_or_need_approval(make_ctx, tools_of, page):
    """Reading a form mutates nothing, so it must not be gated."""
    ctx = make_ctx(on_site=False)  # no NAVIGATE grant at all
    ctx.collab.takeover = True
    result = await (await tools_of(ctx))["read_form"]()

    assert result["status"] == "ok"
    assert not _calls(page, "click")


async def test_read_form_passes_whole_page_scope(make_ctx, tools_of, page):
    """A picker dialog is appended to the body, outside the form it belongs to.

    Measured on KVR: the Image Picker's ``<input type=file>`` lands directly
    under ``body``, so a form-scoped read reports no file input at all.
    """
    tools = await tools_of(make_ctx())
    await tools["read_form"](whole_page=True)

    assert _read_form_scope(page)["whole"] is True


async def test_read_form_defaults_to_the_form_not_the_whole_page(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())
    await tools["read_form"]()

    assert _read_form_scope(page)["whole"] is False


async def test_read_form_is_audited(make_ctx, tools_of):
    ctx = make_ctx()
    await (await tools_of(ctx))["read_form"](root="#product-form")
    entry = next(e for e in _audit(ctx) if e["tool"] == "read_form")
    assert entry["args"]["root"] == "#product-form"


# --------------------------------------------------------------------------
# select_option: a dropdown is not a click target
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"value": "9"}, ("select_option", "#prot", "9", None, None)),
        ({"label": "iLok"}, ("select_option", "#prot", None, "iLok", None)),
        ({"index": 1}, ("select_option", "#prot", None, None, 1)),
    ],
)
async def test_select_option_chooses_by_value_label_or_index(
    make_ctx, tools_of, page, kwargs, expected
):
    tools = await tools_of(make_ctx())
    result = await tools["select_option"](selector="#prot", **kwargs)

    assert result["status"] == "ok"
    assert expected in page.calls


async def test_select_option_refuses_an_ambiguous_choice(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["select_option"](
        selector="#prot", value="9", label="iLok"
    )

    assert result["status"] == "error"
    assert page.calls == [], "nothing may be touched when the instruction is unclear"


async def test_select_option_refuses_no_choice_at_all(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["select_option"](selector="#prot")

    assert result["status"] == "error"
    assert page.calls == []


async def test_select_option_reports_a_missing_control(make_ctx, tools_of, page):
    page.present = {"#prot"}
    result = await (await tools_of(make_ctx()))["select_option"](selector="#nope", value="9")

    assert result["status"] == "not_found"
    assert not _calls(page, "select_option")
    entry = next(e for e in _audit(make_ctx()) if e["tool"] == "select_option")  # shape check
    assert entry["tool"] == "select_option"


async def test_select_option_is_gated_when_it_sends_the_form(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["select_option"](
        selector="#prot", value="9", submits=True
    )

    assert result["status"] == "needs_approval"
    assert page.calls == []


async def test_select_option_proceeds_with_confirm_when_it_submits(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["select_option"](
        selector="#prot", value="9", submits=True, confirm=True
    )

    assert result["status"] == "ok"
    assert _calls(page, "select_option")


async def test_select_option_without_submits_needs_no_approval(make_ctx, tools_of, page):
    """Being on a site lets you use its dropdowns; it does not let you send."""
    ctx = make_ctx()
    result = await (await tools_of(ctx))["select_option"](selector="#prot", value="9")

    assert result["status"] == "ok"
    assert not ctx.perms.check("default", parse_origin(SITE), Capability.SUBMIT)


async def test_select_option_survives_takeover_as_an_envelope(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.collab.takeover = True
    result = await (await tools_of(ctx))["select_option"](selector="#prot", value="9")

    assert result["status"] == "takeover_active"
    assert page.calls == []


# --------------------------------------------------------------------------
# set_editor: the textarea the form posts is not the editing surface
# --------------------------------------------------------------------------


async def test_set_editor_uses_the_editor_iframe_when_none_is_named(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx(editor=True))
    result = await tools["set_editor"](content="Intrect releases de-artifact 0.3.16")

    assert result["status"] == "ok"
    assert ("frame_locator", "iframe.cke_wysiwyg_frame", "body") in page.calls
    assert ("fill", "body", "Intrect releases de-artifact 0.3.16") in page.calls


async def test_set_editor_reports_when_there_is_no_editor(make_ctx, tools_of, page):
    """No editor and no selector is a mistake worth naming, not a silent no-op."""
    page.present = set()  # the page has none of the editor selectors
    result = await (await tools_of(make_ctx()))["set_editor"](content="text")

    assert result["status"] == "not_found"
    assert page.calls == []


async def test_set_editor_writes_the_selector_it_was_given(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())
    result = await tools["set_editor"](content="hello", selector="#bio")

    assert result["status"] == "ok"
    assert ("fill", "#bio", "hello") in page.calls


async def test_set_editor_html_syncs_the_ckeditor_instance(make_ctx, tools_of, page):
    """A description is markup. fill() would escape it, so html writes the body."""
    page.synced_editor = "news"
    tools = await tools_of(make_ctx(editor=True))
    result = await tools["set_editor"](content="<p>Rich</p>", html=True)

    assert result["status"] == "ok"
    assert page.written == "<p>Rich</p>"
    assert result["synced_editor"] == "news"
    assert ("fill", "body", "<p>Rich</p>") not in page.calls, "html must not be escaped by fill"


async def test_set_editor_is_gated_when_it_sends_the_form(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx(editor=True)))["set_editor"](
        content="text", submits=True
    )

    assert result["status"] == "needs_approval"
    # Finding the editor only reads the page (it happens before permission is
    # asked, so a missing editor cannot leave a grant behind). What must not
    # happen without permission is any write.
    assert not [c for c in page.calls if c[0] in {"fill", "sync_editor"}]


async def test_set_editor_survives_takeover_as_an_envelope(make_ctx, tools_of, page):
    ctx = make_ctx(editor=True)
    ctx.collab.takeover = True
    result = await (await tools_of(ctx))["set_editor"](content="text")

    assert result["status"] == "takeover_active"
    assert page.calls == []


async def test_set_editor_audit_carries_the_size_not_the_text(make_ctx, tools_of):
    ctx = make_ctx(editor=True)
    content = "a description nobody needs in the log"
    await (await tools_of(ctx))["set_editor"](content=content)

    # Two entries share the tool name: the consent decision, then the action.
    # The action is the one that says how much was written.
    entries = [e for e in _audit(ctx) if e["tool"] == "set_editor"]
    entry = next(e for e in entries if "content_chars" in e["args"])
    assert entry["args"]["content_chars"] == len(content)
    assert content not in json.dumps(entries)


# --------------------------------------------------------------------------
# upload_file: a file handed to a page cannot be recalled
# --------------------------------------------------------------------------


async def test_upload_file_attaches_paths_and_names_them(make_ctx, tools_of, page, tmp_path):
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    tools = await tools_of(make_ctx())
    result = await tools["upload_file"](selector="#picker", paths=[str(image)], confirm=True)

    assert result["status"] == "ok"
    assert result["files"] == ["logo.png"]
    assert ("set_input_files", "#picker", [str(image)]) in page.calls


async def test_upload_file_accepts_a_hidden_picker(make_ctx, tools_of, page, tmp_path):
    """A styled picker keeps its input hidden; refusing it made uploads impossible."""
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    page.hidden = {"#picker"}
    tools = await tools_of(make_ctx())
    result = await tools["upload_file"](selector="#picker", paths=[str(image)], confirm=True)

    assert result["status"] == "ok"
    assert ("set_input_files", "#picker", [str(image)]) in page.calls


async def test_upload_file_refuses_a_disabled_picker(make_ctx, tools_of, page, tmp_path):
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    page.disabled = {"#picker"}
    tools = await tools_of(make_ctx())
    result = await tools["upload_file"](selector="#picker", paths=[str(image)], confirm=True)

    assert result["status"] == "disabled"
    assert not any(c[0] == "set_input_files" for c in page.calls)


async def test_upload_file_needs_its_own_approval(make_ctx, tools_of, page, tmp_path):
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    result = await (await tools_of(make_ctx()))["upload_file"](
        selector="#picker", paths=[str(image)]
    )

    assert result["status"] == "needs_approval"
    assert page.calls == [], "a gated upload must not leave a picker filled"


async def test_upload_file_reports_missing_paths_before_touching_the_page(
    make_ctx, tools_of, page, tmp_path
):
    present = tmp_path / "there.png"
    present.write_bytes(b"\x89PNG\r\n\x1a\n")
    tools = await tools_of(make_ctx())
    result = await tools["upload_file"](
        selector="#picker", paths=[str(present), str(tmp_path / "gone.png")], confirm=True
    )

    assert result["status"] == "error"
    assert result["missing"] == [str(tmp_path / "gone.png")]
    assert page.calls == [], "one bad path must not attach the good ones anyway"


async def test_upload_file_reports_a_missing_input(make_ctx, tools_of, page, tmp_path):
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    page.present = {"#picker"}
    tools = await tools_of(make_ctx())
    result = await tools["upload_file"](selector="#nope", paths=[str(image)], confirm=True)

    assert result["status"] == "not_found"
    assert not _calls(page, "set_input_files")


async def test_upload_file_waits_for_a_picker_the_page_builds_late(
    make_ctx, tools_of, page, tmp_path, monkeypatch
):
    """KVR's Image Picker attaches its input ~0.5s after load.

    Playwright auto-waits for an *action*, but a presence check answers
    immediately — so asking once called a working page broken (measured in E2E).
    """
    from lyra_browser.tools import actionable

    monkeypatch.setattr(actionable, "_APPEAR_STEP_S", 0.0)
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    page.present = {"#picker"}
    calls = {"n": 0}
    original = FakeLocator.count

    async def late_count(self):
        calls["n"] += 1
        return 1 if calls["n"] > 3 else 0

    monkeypatch.setattr(FakeLocator, "count", late_count)
    monkeypatch.setattr(FakeLocator, "count", late_count, raising=False)
    tools = await tools_of(make_ctx())
    result = await tools["upload_file"](selector="#picker", paths=[str(image)], confirm=True)

    monkeypatch.setattr(FakeLocator, "count", original)
    assert result["status"] == "ok"
    assert _calls(page, "set_input_files")


async def test_upload_file_rejects_an_empty_path_list(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["upload_file"](selector="#picker", paths=[])

    assert result["status"] == "error"
    assert page.calls == []


async def test_upload_approval_is_single_use(make_ctx):
    """A file already handed over is not a licence to hand over the next one."""
    assert Capability.UPLOAD in Capability.__members__.values()
    from lyra_browser.permission import _SINGLE_USE

    assert Capability.UPLOAD in _SINGLE_USE


async def test_upload_file_survives_takeover_as_an_envelope(make_ctx, tools_of, page, tmp_path):
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    ctx = make_ctx()
    ctx.collab.takeover = True
    result = await (await tools_of(ctx))["upload_file"](
        selector="#picker", paths=[str(image)], confirm=True
    )

    assert result["status"] == "takeover_active"
    assert page.calls == []


async def test_upload_file_audits_the_paths_it_gave_away(make_ctx, tools_of, tmp_path):
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    ctx = make_ctx()
    await (await tools_of(ctx))["upload_file"](selector="#picker", paths=[str(image)], confirm=True)

    entries = [e for e in _audit(ctx) if e["tool"] == "upload_file"]
    entry = next(e for e in entries if "files" in e["args"])
    assert entry["args"]["files"] == [str(image)]
    assert entry["args"]["selector"] == "#picker"


# --------------------------------------------------------------------------
# read_draft: everything a human needs to approve the item, in one answer
# --------------------------------------------------------------------------


async def test_read_draft_gathers_fields_editor_attachments_and_state(make_ctx, tools_of):
    result = await (await tools_of(make_ctx()))["read_draft"]()

    assert result["status"] == "ok"
    assert result["title"] == "Start"
    assert any(f["name"] == "head" for f in result["fields"])
    # A hidden field must be reported as unusable, not left to be discovered
    # through a 30s locator timeout.
    hidden = next(f for f in result["fields"] if f["name"] == "event_country")
    assert hidden["usable"] is False, "a section the form has not revealed must say so"
    assert all(f["usable"] for f in result["fields"] if f["name"] != "event_country")
    # The body lives in its own document; read_page never shows it.
    assert "de-artifact" in result["editors"][0]["content"]
    assert result["attachments"][0]["selected"] == ["intrect-logo.png"]
    assert result["state"][0]["label"] == "Draft"
    assert result["isDraft"] is True


async def test_read_draft_is_not_gated(make_ctx, tools_of, page):
    """Reading what a draft says changes nothing."""
    ctx = make_ctx(on_site=False)
    ctx.collab.takeover = True
    result = await (await tools_of(ctx))["read_draft"]()

    assert result["status"] == "ok"
    assert not _calls(page, "check")


async def test_read_draft_is_audited(make_ctx, tools_of):
    ctx = make_ctx()
    await (await tools_of(ctx))["read_draft"]()
    entry = next(e for e in _audit(ctx) if e["tool"] == "read_draft")
    assert entry["args"]["fields"] == 2


# --------------------------------------------------------------------------
# publish: a draft cannot become public without its own approval
# --------------------------------------------------------------------------


async def test_read_form_reports_the_publish_controls(make_ctx, tools_of):
    """An approval check has to see which state the item is in."""
    result = await (await tools_of(make_ctx()))["read_form"]()

    labels = [d["label"] for d in result["drafts"]]
    assert labels == ["Draft", "Publish"]
    assert result["state"] == ["Draft"], "a new item must read as unpublished"
    assert result["submitText"] == ["Submit"]


async def test_publish_refuses_without_both_controls(make_ctx, tools_of, page):
    """Arming the form without sending it is worse than doing nothing.

    Measured on KVR: the publish radio only marks the item; the form's own Submit
    is what sends it. Switching the radio alone would leave the page primed to
    publish on someone else's next click.
    """
    result = await (await tools_of(make_ctx()))["publish"](selector="#live")

    assert result["status"] == "needs_submit"
    assert "submitText" in result["error"]
    assert page.calls == [], "an incomplete publish must touch nothing"


async def test_publish_needs_its_own_approval(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["publish"](selector="#live", submit="#send")

    assert result["status"] == "needs_approval"
    assert page.calls == [], "a refused publish must not flip the control"


async def test_publish_arms_then_sends_and_reports_the_new_state(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())
    result = await tools["publish"](selector="#live", submit="#send", confirm=True)

    assert result["status"] == "ok"
    # Both halves happen, and the send comes after the arm.
    assert ("check", "#live") in page.calls
    assert ("click", "#send") in page.calls
    assert page.calls.index(("check", "#live")) < page.calls.index(("click", "#send"))
    assert result["state"][0]["label"] == "Publish"


async def test_publish_is_never_leased_from_an_earlier_approval(make_ctx, tools_of, page):
    """Approving one item's release must not release the next one."""
    ctx = make_ctx()
    tools = await tools_of(ctx)

    first = await tools["publish"](selector="#live", submit="#send", confirm=True)
    assert first["status"] == "ok"
    second = await tools["publish"](selector="#live", submit="#send")
    assert second["status"] == "needs_approval"


async def test_publish_survives_takeover_as_an_envelope(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.collab.takeover = True
    result = await (await tools_of(ctx))["publish"](selector="#live", submit="#send", confirm=True)

    assert result["status"] == "takeover_active"
    assert page.calls == []


async def test_publish_reports_a_missing_control(make_ctx, tools_of, page):
    page.present = {"#live"}
    result = await (await tools_of(make_ctx()))["publish"](
        selector="#nope", submit="#send", confirm=True
    )

    assert result["status"] == "not_found"
    assert not _calls(page, "check")


async def test_publish_reports_a_missing_submit_control(make_ctx, tools_of, page):
    """A publish that arms the form but has nowhere to send it must not arm it."""
    page.present = {"#live"}
    result = await (await tools_of(make_ctx()))["publish"](
        selector="#live", submit="#send", confirm=True
    )

    assert result["status"] == "not_found"
    assert not _calls(page, "check"), "the form must not be armed with no way to send it"


async def test_select_option_names_a_hidden_control(make_ctx, tools_of, page):
    """A hidden control must not surface as a 30s locator timeout.

    Measured on KVR: ``event_country`` is in the news form's DOM but lives in the
    Deal/Offer section, which stays hidden until that item type is chosen. The
    answer has to name the reason so the caller can fix it.
    """
    page.present = {"#sect"}
    page.hidden = {"#sect"}
    result = await (await tools_of(make_ctx()))["select_option"](selector="#sect", value="1")

    assert result["status"] == "hidden"
    assert result["hint"], "a hidden control must say what to do about it"
    assert ("select_option", "#sect", "1", None, None) not in page.calls


async def test_select_option_reports_a_disabled_control(make_ctx, tools_of, page):
    page.present = {"#sect"}
    page.disabled = {"#sect"}
    result = await (await tools_of(make_ctx()))["select_option"](selector="#sect", value="1")

    assert result["status"] == "disabled"
    assert result["hint"] == ""


async def test_writing_a_draft_never_publishes_it(make_ctx, tools_of, page):
    """The whole point of the split: filling a form is not releasing it.

    Every writing tool must be usable to completion without ``publish`` being
    called, so an operator can review the item before anything goes live.
    """
    tools = await tools_of(make_ctx(editor=True))

    assert (await tools["type_text"](selector="#head", value="Headline"))["status"] == "ok"
    assert (await tools["set_editor"](content="<p>Body</p>", html=True))["status"] == "ok"
    assert (await tools["select_option"](selector="#sect", value="1"))["status"] == "ok"

    assert not _calls(page, "check"), "no writing tool may release the item"
    assert page.publish_checked == ""
    assert not _calls(page, "click"), "no writing tool may send the form either"


async def test_publish_audit_names_the_control_and_the_reason(make_ctx, tools_of):
    ctx = make_ctx()
    await (await tools_of(ctx))["publish"](
        selector="#live", submit="#send", reason="운영자 승인", confirm=True
    )

    entries = [e for e in _audit(ctx) if e["tool"] == "publish"]
    entry = next(e for e in entries if "selector" in e["args"])
    assert entry["args"]["selector"] == "#live"
    assert entry["args"]["submit"] == "#send"
    assert entry["args"]["reason"] == "운영자 승인"


async def test_save_draft_sends_a_form_the_page_still_has_private(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["save_draft"](submit="#send", confirm=True)

    assert result["status"] == "ok"
    assert ("click", "#send") in page.calls
    assert result["published"] is False
    assert result["state"][0]["label"] == "Draft"


async def test_save_draft_refuses_a_form_armed_to_publish(make_ctx, tools_of, page):
    """The whole safety property: saving must not be a way to publish.

    A page left armed by anything else must not slip out through this tool, and
    the caller cannot talk it into sending — the state is read off the page.
    """
    page.published_state = page.live_state
    result = await (await tools_of(make_ctx()))["save_draft"](submit="#send", confirm=True)

    assert result["status"] == "not_private"
    assert not _calls(page, "click"), "an armed form must not be sent"


async def test_save_draft_refuses_a_state_it_cannot_classify(make_ctx, tools_of, page):
    """A control nobody can read is not a safe one — refuse rather than guess."""
    page.published_state = [{"name": "status", "value": "7", "label": ""}]
    result = await (await tools_of(make_ctx()))["save_draft"](submit="#send", confirm=True)

    assert result["status"] == "not_private"
    assert not _calls(page, "click")


async def test_save_draft_refuses_when_the_page_reports_no_publish_control(
    make_ctx, tools_of, page
):
    page.published_state = []
    result = await (await tools_of(make_ctx()))["save_draft"](submit="#send", confirm=True)

    assert result["status"] == "not_private"
    assert not _calls(page, "click")


async def test_save_draft_needs_the_send_control(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["save_draft"]()

    assert result["status"] == "needs_submit"
    assert page.calls == []


async def test_save_draft_is_gated_and_reports_its_refusal(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["save_draft"](submit="#send")

    assert result["status"] == "needs_approval"
    assert not _calls(page, "click")


async def test_save_draft_is_audited_with_the_state_it_checked(make_ctx, tools_of):
    ctx = make_ctx()
    await (await tools_of(ctx))["save_draft"](submit="#send", reason="초안 저장", confirm=True)

    entry = next(e for e in _audit(ctx) if e["tool"] == "save_draft" and "reason" in e["args"])
    assert entry["args"]["submit"] == "#send"
    assert entry["args"]["state"][0]["label"] == "Draft"


async def test_reads_as_private_classifies_the_labels_a_page_uses():
    """One rule has to work on both KVR forms, which name the group differently."""
    from lyra_browser.tools.forms import _reads_as_private

    # KVR news: is_draft, labelled Draft / Publish.
    assert _reads_as_private({"name": "is_draft", "value": "1", "label": "Draft"}) is True
    assert _reads_as_private({"name": "is_draft", "value": "0", "label": "Publish"}) is False
    # KVR product: is_live, where 0 is the private setting.
    assert _reads_as_private({"name": "is_live", "value": "0", "label": "Draft"}) is True
    assert _reads_as_private({"name": "is_live", "value": "1", "label": "Live"}) is False
    # A label wins over a group name that describes the pair, not the option.
    assert _reads_as_private({"name": "is_draft", "value": "0", "label": "Publish"}) is False
    # Unclassifiable is not private.
    assert _reads_as_private({"name": "status", "value": "7", "label": ""}) is False


async def test_save_draft_reports_a_refused_save_rather_than_claiming_success(
    make_ctx, tools_of, page
):
    """A click succeeding means the element was clicked, not that the server took it.

    Measured on KVR: the server answered "The News Submission form appeared to be
    incomplete or had errors" while the tool reported ok and nothing was saved.
    """
    page.after_send = {
        "formPresent": True,
        "formAction": "",
        "fields": 182,
        "errors": [
            "The News Submission form appeared to be incomplete or had errors\n\n"
            "Choose at least one News Category / Section\n"
            "Add the News Item content (Min 50 characters)"
        ],
        "alert": "The News Submission form appeared to be incomplete or had errors",
        "refused": True,
    }
    result = await (await tools_of(make_ctx()))["save_draft"](submit="#send", confirm=True)

    assert result["status"] == "refused", "an ok here would read as a successful save"
    assert result["formPresent"] is True
    assert any("incomplete" in e for e in result["errors"])
    assert result["published"] is False


async def test_save_draft_reports_the_page_after_it_sends(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["save_draft"](submit="#send", confirm=True)

    assert result["errors"] == []
    assert result["formPresent"] is False


# --------------------------------------------------------------------------
# set_editor: the editor looking filled is not the form having the content
# --------------------------------------------------------------------------


async def test_set_editor_syncs_the_field_the_form_posts(make_ctx, tools_of, page):
    """CKEditor needs updateElement(), or the save sends an empty body.

    Measured on KVR: setData() filled the editing surface while the posted
    ``textarea[name=news]`` stayed at 0 chars, and the server refused the save for
    missing content. The tool reported ok both times.
    """
    page.synced_editor = ""
    result = await (await tools_of(make_ctx(editor=True)))["set_editor"](
        content="<p>Body text</p>", html=True, confirm=True
    )

    assert result["status"] == "ok"
    assert ("sync_editor",) in [(c[0],) for c in page.calls], "the posted field must be synced"
    assert result["field_chars"] == page.editor_field_chars


async def test_set_editor_reports_the_posted_field_length(make_ctx, tools_of, page):
    """The caller needs the posted field's size, not the editor's, to trust a save."""
    page.editor_field_chars = 128
    result = await (await tools_of(make_ctx(editor=True)))["set_editor"](
        content="<p>Something long enough</p>", html=True, confirm=True
    )

    assert result["synced_field"] == "news"
    assert result["field_chars"] == 128


async def test_set_editor_syncs_after_a_plain_text_write_too(make_ctx, tools_of, page):
    """Both write paths leave the posted field empty; both must ask for the sync."""
    result = await (await tools_of(make_ctx(editor=True)))["set_editor"](
        content="plain text", html=False, confirm=True
    )

    assert result["status"] == "ok"
    assert ("sync_editor",) in [(c[0],) for c in page.calls]
    assert result["field_chars"] == page.editor_field_chars


async def test_publish_is_single_use_and_implied_by_nothing(make_ctx):
    from lyra_browser.permission import _IMPLIED, _SINGLE_USE

    assert Capability.PUBLISH in _SINGLE_USE
    assert Capability.PUBLISH not in _IMPLIED.get(Capability.NAVIGATE, ())
    assert Capability.PUBLISH not in _IMPLIED.get(Capability.INTERACT, ())


# A one-shot grant belongs to the action that bought it. A control that cannot be
# used must be rejected before permission is asked, so nothing is bought and
# nothing is left live for a later request from the same origin to spend.
_ONE_SHOT = {Capability.SUBMIT, Capability.UPLOAD, Capability.PUBLISH}


def _live_one_shots(ctx) -> list:
    from lyra_browser.context import current_session_key

    return [
        g
        for g in ctx.perms.live_grants(current_session_key(ctx))
        if g.uses_left and g.capability in _ONE_SHOT
    ]


async def test_select_option_on_a_hidden_control_leaves_no_grant_behind(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.present = {"#sect"}
    page.hidden = {"#sect"}
    result = await (await tools_of(ctx))["select_option"](
        selector="#sect", value="1", submits=True, confirm=True
    )

    assert result["status"] == "hidden"
    assert not _calls(page, "select_option")
    assert _live_one_shots(ctx) == []


@pytest.mark.parametrize("failure", ["missing", "disabled"])
async def test_upload_file_on_an_unusable_input_leaves_no_grant_behind(
    make_ctx, tools_of, page, tmp_path, failure
):
    ctx = make_ctx()
    image = tmp_path / "logo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    page.present = {"#picker"}
    if failure == "disabled":
        page.disabled = {"#picker"}
    selector = "#picker" if failure == "disabled" else "#nope"
    result = await (await tools_of(ctx))["upload_file"](
        selector=selector, paths=[str(image)], confirm=True
    )

    assert result["status"] == ("disabled" if failure == "disabled" else "not_found")
    assert not _calls(page, "set_input_files")
    assert _live_one_shots(ctx) == []


@pytest.mark.parametrize("broken", ["target", "submit"])
async def test_publish_with_a_hidden_control_leaves_no_grant_behind(
    make_ctx, tools_of, page, broken
):
    ctx = make_ctx()
    page.present = {"#live", "#send"}
    page.hidden = {"#live"} if broken == "target" else {"#send"}
    result = await (await tools_of(ctx))["publish"](selector="#live", submit="#send", confirm=True)

    assert result["status"] == "hidden"
    assert not _calls(page, "check")
    assert not _calls(page, "click")
    assert _live_one_shots(ctx) == []


async def test_set_editor_with_no_editor_leaves_no_grant_behind(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.present = set()  # none of the editor selectors exist
    result = await (await tools_of(ctx))["set_editor"](content="text", submits=True, confirm=True)

    assert result["status"] == "not_found"
    assert not [c for c in page.calls if c[0] in {"fill", "sync_editor"}]
    assert _live_one_shots(ctx) == []


async def test_an_unusable_control_does_not_prompt_for_a_one_shot_grant(make_ctx, tools_of, page):
    """Nobody should be asked to approve an action that could not run."""
    ctx = make_ctx()
    page.present = {"#live", "#send"}
    page.hidden = {"#live"}
    result = await (await tools_of(ctx))["publish"](selector="#live", submit="#send")

    assert result["status"] == "hidden"
