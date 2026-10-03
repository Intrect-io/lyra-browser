"""Filling a real form: reading its fields, choosing options, writing rich text,
and handing a file to a page.

The interaction tools cover the two things every page has — a click and a
keystroke. A form on a real site needs three more, and each was measured on KVR's
developer area before it was written here:

- **Dropdowns.** ``<select>`` is not a click target. Playwright's ``select_option``
  sets the value and fires ``change``; clicking the option's text is not the same
  action and does not work on a native control.
- **Rich text.** KVR's news and product forms each carry a CKEditor 4 instance
  whose body lives in an iframe (``.cke_wysiwyg_frame``). The ``<textarea>`` the
  form actually posts is hidden and empty — ``type_text`` on it times out — so the
  text has to go to the editing surface. Measured: ``fill()`` on that iframe body
  does sync back to ``CKEDITOR.instances[…].getData()``.
- **File pickers.** KVR's Image Picker creates ``<input type=file
  accept=".jpg,.gif,.png,.jpeg,.webp" multiple>`` when it is opened, and the
  styled picker keeps it hidden. ``set_input_files`` works on a hidden input; it
  is the only way to attach an image.

``read_form`` exists because the same measurement showed what ``read_page``
cannot do: it returns ``innerText``, so a 188-field product form arrives as a
wall of labels with no ``name``, no ``type``, and no ``<select>`` options — every
one of which is needed to address a field at all.

None of this decides what a request *does*. These tools declare intent
(``submits=true``) exactly like ``click``, and ``enforcement.py`` judges what
leaves. Attaching a file buys ``UPLOAD``, which is single-use because a file
handed to a page cannot be recalled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastmcp import FastMCP

from ..approval import TakeoverActive
from ..context import ServerContext, acquire_page
from ..permission import Capability
from .actionable import _actionable, _appears
from .scope import page_origin, released, require

# Bodies that carry rich text, most specific first. An iframe wins over a
# ``div[contenteditable]`` because a page may keep both, and the iframe is where
# the editor actually writes.
_EDITOR_FRAMES = ("iframe.cke_wysiwyg_frame", "iframe.tox-edit-area__iframe")
_EDITOR_EDITABLES = ("[contenteditable=true]", "div.ql-editor", ".ProseMirror")


def _scopes(extra: Capability | None) -> list[Capability]:
    """Interacting is always needed; another effect is added when declared."""
    return [Capability.INTERACT, extra] if extra is not None else [Capability.INTERACT]


# Reading a form is the one thing here that is not an action. The shape is
# deliberately the same as the enforcement layer's vocabulary — name, type,
# options, required — because those are what address a field.
#
# Takes a single object: ``page.evaluate`` passes exactly one argument, and
# splitting root from limit across two would have silently dropped the limit.
_FORM_JS = """({root, limit, whole}) => {
  const scope = whole ? document : (document.querySelector(root) || document);
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    const holder = el.closest('label');
    if (holder && holder.innerText.trim()) return holder.innerText.trim();
    const row = el.closest('tr,li,dd,p');
    if (row) {
      const copy = row.cloneNode(true);
      copy.querySelectorAll('input,select,textarea,button,script,style')
        .forEach((n) => n.remove());
      const text = copy.innerText.trim();
      if (text) return text.slice(0, 160);
    }
    return '';
  };
  const describe = (el) => {
    const out = {
      tag: el.tagName.toLowerCase(),
      type: el.type || '',
      name: el.name || '',
      id: el.id || '',
      label: labelFor(el),
      required: !!el.required,
      disabled: !!el.disabled,
      // Whether this control can actually be used now. A form keeps sections it
      // has not revealed — measured on KVR: event_country sits in the Deal/Offer
      // block, hidden until that type is chosen. Without this, picking it looks
      // exactly like picking a live field and fails 30s later.
      usable: !el.disabled && !!(el.offsetParent || el.getClientRects().length),
    };
    if (el.tagName === 'SELECT') {
      out.value = el.value;
      out.options = Array.from(el.options).slice(0, 60).map((o) => ({
        value: o.value, text: o.text.trim().slice(0, 80),
      }));
    } else if (el.type === 'file') {
      out.accept = el.accept || '';
      out.multiple = !!el.multiple;
    } else if (el.type === 'checkbox' || el.type === 'radio') {
      out.checked = !!el.checked;
      out.value = el.value;
    } else if (el.type !== 'hidden') {
      out.value = (el.value || '').slice(0, 120);
    }
    return out;
  };
  const fields = Array.from(scope.querySelectorAll('input,select,textarea'))
    .filter((el) => el.type !== 'hidden')
    .slice(0, limit)
    .map(describe);
  const hidden = Array.from(scope.querySelectorAll('input[type=hidden]')).length;
  const buttons = Array.from(
    scope.querySelectorAll('button,input[type=submit],input[type=image]'))
    .slice(0, 40)
    .map((b) => ({
      tag: b.tagName.toLowerCase(), type: b.type || '', name: b.name || '',
      value: b.value || '', text: (b.innerText || '').trim().slice(0, 60),
    }));
  const editors = Array.from(scope.querySelectorAll(
    'iframe.cke_wysiwyg_frame,iframe.tox-edit-area__iframe,'
    + '[contenteditable=true],div.ql-editor,.ProseMirror'))
    .slice(0, 10)
    .map((el) => {
      const out = {
        tag: el.tagName.toLowerCase(), id: el.id || '',
        cls: (el.className || '').toString().slice(0, 80),
      };
      // What the editor currently holds, so a write can be checked. This script
      // runs in the main frame, so CKEDITOR is right here — unlike inside the
      // editor's own iframe body, where it lives on the window above it.
      try {
        if (window.CKEDITOR && window.CKEDITOR.instances) {
          for (const key of Object.keys(window.CKEDITOR.instances)) {
            const editor = window.CKEDITOR.instances[key];
            if (editor.editable && editor.editable.$ === el) {
              out.instance = key;
              out.content = (editor.getData() || '').slice(0, 400);
              break;
            }
          }
        }
      } catch (err) { /* an editor that will not answer is still worth listing */ }
      if (out.content === undefined) {
        try {
          const doc = el.tagName.toLowerCase() === 'iframe' ? el.contentDocument : el.ownerDocument;
          const body = el.tagName.toLowerCase() === 'iframe' ? doc.body : el;
          out.content = (body.innerText || '').trim().slice(0, 400);
        } catch (err) { /* cross-origin frame */ }
      }
      return out;
    });
  const drafts = Array.from(scope.querySelectorAll('input[type=radio],input[type=checkbox]'))
    .filter((el) => {
      // Group by name so a 20-option radio set is reported once, then keep only
      // groups whose labels read as a publish state. The names differ per site
      // (KVR news: is_draft; KVR product: is_live), so the label is what decides.
      const group = (el.closest('label')?.innerText || '').trim()
        + ' ' + (el.name || '') + ' ' + (el.id || '');
      return /draft|publish|live|unpublish|private|hidden/i.test(group);
    })
    .slice(0, 20)
    .map((el) => ({
      name: el.name || '', value: el.value || '', checked: !!el.checked,
      label: (el.closest('label')?.innerText || '').trim().slice(0, 40),
    }));
  // The state a form is in as it stands: the checked member of a publish group.
  // Measured on KVR: a new item opens on Draft and only a human switching that
  // radio to Publish makes it public — so this is what an approval check reads.
  const state = drafts.filter((d) => d.checked).map((d) => d.label || d.value);
  const submitText = buttons.filter((b) => b.type === 'submit')
    .map((b) => b.text || b.value).filter(Boolean);
  return {
    fields, buttons, editors, drafts, state, submitText,
    hiddenCount: hidden, fieldCount: fields.length,
  };
}"""

# Writing HTML: ``fill`` would escape it, and a description is meant to be
# markup. Set the body and tell the page it changed, then hand the same value to
# a CKEditor 4 instance when this body belongs to one, so the hidden textarea the
# form posts is updated too.
#
# The instance is only reachable from the window that owns the editor — for a
# framed body that is the PARENT window, and ``editor.setData()`` writes to the
# editing surface while the form posts the ``<textarea>`` the editor was built
# from. ``updateElement()`` fills that field. Measured on KVR: with setData()
# alone the editor looked right, the posted field stayed at 0 chars, and the
# server answered "Add the News Item content (Min 50 characters)" — so this runs
# in the parent page, keyed by the editor's own field name, rather than trying to
# match the instance from inside the frame.
_SET_HTML_JS = """(el, html) => {
  el.innerHTML = html;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  // Which field this editing surface belongs to, so the parent can find the
  // instance without comparing editable objects across frame boundaries.
  let fieldName = '';
  try {
    const doc = el.ownerDocument;
    const frame = doc.defaultView && doc.defaultView.frameElement;
    if (frame && frame.id) fieldName = frame.id;
  } catch (err) { /* cross-origin frame */ }
  if (!fieldName) fieldName = el.id || '';
  return {written: el.innerHTML.length, editorField: fieldName};
}"""

# Filling the field a form posts after the editor was written. Keyed by the
# editor's own name, and run against the top document so the instance is found
# wherever it lives. On a page with one editor the name is optional.
#
# ``updateElement()`` is the whole point: CKEditor writes the editing surface from
# setData(), and only this copies it into the ``<textarea>`` the form submits.
_SYNC_EDITOR_JS = """({field}) => {
  const out = {synced: '', syncedField: '', fieldChars: 0, editorChars: 0, found: []};
  if (!window.CKEDITOR || !window.CKEDITOR.instances) return out;
  const keys = Object.keys(window.CKEDITOR.instances);
  const nameOf = (ed) => (ed.element && ed.element.$.name) || ed.name || '';
  for (const k of keys) {
    out.found.push({key: k, name: nameOf(window.CKEDITOR.instances[k])});
  }
  // Prefer the named editor; fall back to the only one on the page.
  let key = '';
  if (field) {
    key = keys.find((k) => nameOf(window.CKEDITOR.instances[k]) === field
      || k === field) || '';
  }
  if (!key && keys.length === 1) key = keys[0];
  if (!key) return out;
  const editor = window.CKEDITOR.instances[key];
  editor.updateElement();
  const f = editor.element && editor.element.$;
  out.synced = key;
  out.syncedField = f ? (f.name || '') : '';
  out.fieldChars = f ? (f.value || '').length : 0;
  out.editorChars = (editor.getData() || '').length;
  return out;
}"""

# What the page says after a form was sent. A server that refuses re-renders the
# same form with its reasons; one that accepts moves on. Reading only the click's
# success (which means "the element was clicked") cannot tell those apart —
# measured on KVR, a rejected save and an accepted one both left the tool on the
# same URL with a 200 response.
#
# The reasons land in the site's own alert container (``#kvr-alert``), so the
# container is named rather than guessed at from a class list: on KVR the page's
# status banner ("NOT LIVE") carries an ``error`` class in the *normal* state, so
# scanning by class reports a failure on a healthy form.
_AFTER_SEND_JS = """() => {
  const out = {formPresent: false, formAction: '', fields: 0, errors: [], alert: ''};
  const form = document.querySelector('form');
  out.formPresent = !!form;
  out.formAction = form ? (form.getAttribute('action') || '') : '';
  out.fields = form ? form.querySelectorAll('input,select,textarea').length : 0;
  // The site's alert box, and any explicit error container, in that order.
  const boxes = [
    document.getElementById('kvr-alert'),
    document.querySelector('[class*=alertbox]'),
    document.querySelector('[class*=errorbox]'),
  ];
  for (const box of boxes) {
    if (!box) continue;
    const text = (box.innerText || box.textContent || '').trim();
    // Only a visible box that actually says something counts.
    if (!text || !(box.offsetParent || box.getClientRects().length)) continue;
    const clean = text.replace(/\\s*OK\\s*$/, '').trim();
    if (clean && !out.errors.includes(clean)) out.errors.push(clean);
    if (!out.alert) out.alert = clean.slice(0, 400);
  }
  // A refusal names what to fix; the site's wording is stable enough to catch.
  const joined = out.errors.join(' ').toLowerCase();
  out.refused = /appeared to be incomplete|had errors|not accepted/.test(joined)
    || /choose at least one|add the .* content/.test(joined);
  return out;
}"""

# What a form is about to publish, gathered in one pass. A reviewer needs all of
# it: the labelled fields, each editor's real content (its own document), the
# attachment slots, the submit control, and whether the item is a draft or live.
_DRAFT_JS = """({limit}) => {
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    const holder = el.closest('label');
    if (holder && holder.innerText.trim()) return holder.innerText.trim();
    const row = el.closest('tr,li,dd,p');
    if (row) {
      const copy = row.cloneNode(true);
      copy.querySelectorAll('input,select,textarea,button,script,style')
        .forEach((n) => n.remove());
      const text = copy.innerText.trim();
      if (text) return text.slice(0, 160);
    }
    return '';
  };
  const fields = [];
  document.querySelectorAll('input,select,textarea').forEach((el) => {
    if (el.type === 'hidden' || el.type === 'file') return;
    if (el.type === 'checkbox' || el.type === 'radio') {
      if (!el.checked) return;
      fields.push({name: el.name || '', label: labelFor(el).slice(0, 90), value: el.value || '',
                   usable: !el.disabled && !!(el.offsetParent || el.getClientRects().length)});
      return;
    }
    const value = (el.tagName === 'SELECT' ? el.selectedOptions[0]?.text : el.value) || '';
    if (!value.trim()) return;
    fields.push({name: el.name || '', label: labelFor(el).slice(0, 90),
                 value: String(value).slice(0, limit),
                 usable: !el.disabled && !!(el.offsetParent || el.getClientRects().length)});
  });
  const editors = [];
  document.querySelectorAll(
    'iframe.cke_wysiwyg_frame,iframe.tox-edit-area__iframe,[contenteditable=true],'
    + 'div.ql-editor,.ProseMirror').forEach((el) => {
    let content = '';
    let instance = '';
    try {
      if (window.CKEDITOR && window.CKEDITOR.instances) {
        for (const key of Object.keys(window.CKEDITOR.instances)) {
          const ed = window.CKEDITOR.instances[key];
          if (ed.editable && ed.editable.$ === el) {
            instance = key;
            content = ed.getData() || '';
            break;
          }
        }
      }
    } catch (err) { /* an editor that will not answer is still worth listing */ }
    if (!content) {
      try {
        const framed = el.tagName.toLowerCase() === 'iframe';
        const body = framed ? el.contentDocument.body : el;
        content = framed ? (body.innerHTML || '') : (body.innerText || '');
      } catch (err) { /* cross-origin frame */ }
    }
    editors.push({instance, chars: content.length, content: content.slice(0, limit * 4)});
  });
  const attachments = Array.from(document.querySelectorAll('input[type=file]')).map((el) => ({
    name: el.name || '', accept: el.accept || '', multiple: !!el.multiple,
    selected: el.files ? Array.from(el.files).map((f) => f.name) : [],
  }));
  const state = [];
  document.querySelectorAll('input[type=radio],input[type=checkbox]').forEach((el) => {
    if (!el.checked) return;
    const label = (el.closest('label')?.innerText || '').trim();
    const group = label + ' ' + (el.name || '') + ' ' + (el.id || '');
    if (/draft|publish|live|unpublish|private|hidden/i.test(group)) {
      state.push({name: el.name || '', value: el.value || '', label: label.slice(0, 40)});
    }
  });
  const submitSel = 'button,input[type=submit],input[type=image]';
  const submits = Array.from(document.querySelectorAll(submitSel))
    .map((b) => ({text: (b.innerText || '').trim().slice(0, 50), name: b.name || ''}))
    .filter((b) => b.text || b.name);
  return {
    fields, editors, attachments, state, submits,
    fieldCount: fields.length,
    isDraft: state.some((s) => /draft/i.test(s.label + s.value)),
  };
}"""

_IS_FRAME_JS = "el => el.tagName.toLowerCase() === 'iframe'"

# What the page now shows about an item's visibility, read straight off the
# checked members of its publish group. Reporting this back is what lets a caller
# show the human the change that happened instead of the one that was requested.
_PUBLISH_STATE_JS = """() => {
  const on = [];
  document.querySelectorAll('input[type=radio],input[type=checkbox]').forEach((el) => {
    if (!el.checked) return;
    const label = (el.closest('label')?.innerText || '').trim();
    const group = label + ' ' + (el.name || '') + ' ' + (el.id || '');
    if (/draft|publish|live|unpublish|private|hidden/i.test(group)) {
      on.push({name: el.name || '', value: el.value || '', label: label.slice(0, 40)});
    }
  });
  return on;
}"""

# How a checked publish control reads. The **label** decides, because a radio
# group labels each option ("Draft" / "Publish" on KVR) while the group's *name*
# describes the pair rather than the option — ``is_draft`` appears on both the
# draft and the publish member, so a name-first rule calls the publish option
# private. The name and value are the fallback, for a form whose options have no
# label at all.
_PRIVATE_WORDS = ("draft", "unpublish", "private", "hidden", "off")
_LIVE_WORDS = ("publish", "live", "public", "visible", "online")
_PRIVATE_VALUES = ("1", "true", "yes", "draft", "private", "hidden", "unpublish")


def _reads_as_private(member: dict) -> bool:
    """Whether a checked control says the item is *not* public.

    ``save_draft`` only sends when this is true, so the answer has to be a
    positive identification of a private state rather than the absence of an
    obvious ``Publish`` — a control nobody can classify is not a safe one.
    """
    label = str(member.get("label", "")).strip().lower()
    if label:
        if any(word in label for word in _PRIVATE_WORDS):
            return True
        if any(word in label for word in _LIVE_WORDS):
            return False
    name = str(member.get("name", "")).strip().lower()
    value = str(member.get("value", "")).strip().lower()
    if "draft" in name or "private" in name or "hidden" in name:
        return value in ("1", "true", "yes", "draft")
    if "live" in name or "publish" in name or "visible" in name:
        return value in ("0", "false", "no", "draft")
    return value in _PRIVATE_VALUES


# How long to let a sent form answer before reading the result back. Measured on
# KVR: a rejected save re-renders within a second or two; an accepted one starts
# navigating. Long enough to catch the first, short enough not to stall the call.
_SETTLE_MS = 2500
_SETTLE_BUDGET_S = 3.0


async def _await_settle(page, dialogs: list | None = None) -> dict:
    """What the page says once the form has been sent.

    A refused submission reports its reasons on the page rather than in the URL, so
    waiting a moment and reading it back is what separates "saved" from "the button
    was clicked". A refusal answers with ``status="refused"`` plus the site's own
    wording, because a caller that only sees ``ok`` cannot tell the difference —
    measured on KVR, both outcomes left the page on the same URL.
    """
    try:
        await page.wait_for_timeout(_SETTLE_MS)
    except Exception:  # noqa: BLE001 — a page mid-navigation still has to be read
        await asyncio.sleep(_SETTLE_BUDGET_S)
    after: dict = {}
    try:
        after = await page.evaluate(_AFTER_SEND_JS) or {}
    except Exception:  # noqa: BLE001 — a page that will not answer is not a success
        after = {}
    if dialogs:
        after["errors"] = list(dialogs) + list(after.get("errors") or [])
        after["refused"] = True
    if after.get("refused"):
        after["status"] = "refused"
    return after


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def read_form(
        root: str = "form", whole_page: bool = False, max_fields: int = 200
    ) -> dict:
        """List a form's fields with their names, types, labels and options.

        ``read_page`` returns rendered text, which loses everything needed to
        address a field: a large form arrives as labels with no ``name``, and a
        ``<select>``'s options vanish. Use this before filling a form you have
        not seen. ``root`` is a CSS selector for the form (default: the first
        form on the page). Hidden inputs are counted, not listed.

        Set ``whole_page=true`` for controls a page attached outside the form —
        a picker or editor dialog is often appended to the body, and a field
        found there is still one the form will post.

        Each field carries ``usable``: false means it is in the DOM but hidden or
        disabled right now, so choosing it will fail. A form hides sections it has
        not revealed yet — measured on KVR, a news form keeps ``event_country``
        hidden until the Deal/Offer type is chosen. Check ``usable`` before
        addressing a field rather than discovering it through a timeout.
        """
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        data = await page.evaluate(
            _FORM_JS,
            {"root": root, "limit": max(1, min(max_fields, 500)), "whole": whole_page},
        )
        ctx.audit.record("read_form", {"root": root, "fields": data.get("fieldCount")})
        return {"status": "ok", "url": page.url, **data}

    @mcp.tool
    async def select_option(
        selector: str,
        value: str = "",
        label: str = "",
        index: int = -1,
        submits: bool = False,
        reason: str = "",
        confirm: bool = False,
    ) -> dict:
        """Choose an option in a ``<select>``, by ``value``, ``label`` or ``index``.

        Give exactly one of the three. A dropdown is not a click target: this
        sets the value and fires ``change``, which is what a page listens for.
        Set ``submits=true`` when the choice sends the form (an ``onchange``
        that posts) — that asks for the permission it needs.
        """
        chosen = [
            n
            for n, v in (("value", value), ("label", label), ("index", index))
            if v != "" and v != -1
        ]
        if len(chosen) != 1:
            return {
                "status": "error",
                "reason": "Give exactly one of value, label or index.",
                "given": chosen,
            }
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        # Usability is checked before permission is asked: a hidden control
        # should not prompt, and a one-shot SUBMIT bought for it would outlive
        # this call.
        target = page.locator(selector).first
        unusable = await _actionable(target)
        if unusable:
            ctx.audit.record(
                "select_option", {"selector": selector}, status=unusable, origin=origin.describe()
            )
            return {
                "status": unusable,
                "selector": selector,
                "url": page.url,
                "hint": (
                    "The control exists but cannot be used as it is — a section "
                    "that is hidden until its type is chosen is the common case. "
                    "Make it visible first, then choose."
                )
                if unusable == "hidden"
                else "",
            }
        denied, bought = await require(
            ctx,
            "select_option",
            target=origin,
            capabilities=_scopes(Capability.SUBMIT if submits else None),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            return denied
        args: dict = {"selector": selector}
        async with released(ctx, bought):
            if value:
                args["value"] = value
                await target.select_option(value=value)
            elif label:
                args["label"] = label
                await target.select_option(label=label)
            else:
                args["index"] = index
                await target.select_option(index=index)
            picked = await target.input_value()
            ctx.audit.record("select_option", args, origin=origin.describe())
            return {"status": "ok", "url": page.url, "selected": picked}

    @mcp.tool
    async def set_editor(
        content: str,
        selector: str = "",
        html: bool = False,
        submits: bool = False,
        reason: str = "",
        confirm: bool = False,
    ) -> dict:
        """Write into a rich-text editor — the body a form actually posts.

        For CKEditor, TinyMCE and similar the visible editor is an ``iframe``
        whose body is editable, and the ``<textarea>`` the form submits is hidden
        and empty: ``type_text`` on that textarea does nothing. Leave ``selector``
        empty to use the first editor on the page, or pass the editor's own
        ``iframe`` or ``[contenteditable]`` element.

        ``html=true`` writes markup (links, paragraphs, images) and syncs the
        editor's own data when it is a CKEditor instance; otherwise the text is
        written verbatim. Set ``submits=true`` if the write sends the form.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        # Resolving the editor only reads the page, so it happens before
        # permission is asked: a one-shot grant bought for a page with no editor
        # would outlive this call.
        resolved = selector
        if not resolved:
            for candidate in (*_EDITOR_FRAMES, *_EDITOR_EDITABLES):
                # A script-built editor may not be attached yet — waiting is not
                # the same as inventing one, and read_form reports what it saw.
                if await _appears(page.locator(candidate).first):
                    resolved = candidate
                    break
        if not resolved:
            ctx.audit.record(
                "set_editor",
                {"content_chars": len(content)},
                status="not_found",
                origin=origin.describe(),
            )
            return {
                "status": "not_found",
                "reason": "No editor found on this page; pass its selector.",
                "url": page.url,
            }

        # The editing surface: the iframe's body when the editor is framed, the
        # element itself when it is not. ``fill`` reaches a body inside an
        # iframe that is same-origin, which is why the frame is entered rather
        # than written from the outside.
        if await page.locator(resolved).first.evaluate(_IS_FRAME_JS):
            body = page.frame_locator(resolved).locator("body")
            surface = f"{resolved} > body"
        else:
            body = page.locator(resolved).first
            surface = resolved

        denied, bought = await require(
            ctx,
            "set_editor",
            target=origin,
            capabilities=_scopes(Capability.SUBMIT if submits else None),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            return denied

        audit = {"selector": resolved, "html": html, "content_chars": len(content)}
        async with released(ctx, bought):
            written: dict = {}
            field = ""
            if html:
                written = await body.evaluate(_SET_HTML_JS, content) or {}
                field = written.get("editorField", "")
            else:
                await body.fill(content)
            # Copy the edit into the field the form posts. CKEditor writes the
            # editing surface from setData()/its own listeners and leaves that
            # hidden textarea alone, so a save would send an empty body. Measured
            # on KVR: the editor read 231 chars while the posted field stayed 0,
            # and the server refused the save for missing content.
            #
            # Run against the top document: the instance lives on the window that
            # owns the editor, which for a framed body is the parent, and matching
            # it from inside the frame does not work.
            top = page.locator("html").first
            synced = await top.evaluate(_SYNC_EDITOR_JS, {"field": field}) or {}
            ctx.audit.record("set_editor", audit, origin=origin.describe())
            result = {"status": "ok", "url": page.url, "surface": surface}
            if synced.get("synced"):
                result["synced_editor"] = synced["synced"]
                result["synced_field"] = synced.get("syncedField", "")
                # The length of the field the form posts, not of the editor: an
                # editor that looks filled while this is 0 sends nothing, which is
                # how a save gets refused for missing content.
                result["field_chars"] = synced.get("fieldChars", 0)
            result["written"] = written.get("written") or len(content)
            return result

    @mcp.tool
    async def upload_file(
        selector: str,
        paths: list[str],
        reason: str = "",
        confirm: bool = False,
    ) -> dict:
        """Attach one or more local files to a file picker.

        This hands a file on this machine to a page, which is why it asks for its
        own permission, once per call. ``selector`` is the ``input[type=file]``;
        a styled picker often keeps it hidden, and that is fine — a hidden input
        still accepts files. Missing paths are reported before anything is
        attached, so a gated call never leaves a picker half-filled.
        """
        wanted = [Path(p) for p in paths if p]
        missing = [str(p) for p in wanted if not p.is_file()]

        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        # Whose turn it is comes before whether the arguments are good: a user
        # driving must hear "the session is taken over", not a complaint about a
        # path. Validation has no side effect, so nothing is lost by waiting.
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        if not wanted:
            return {"status": "error", "reason": "Give at least one path in paths."}
        if missing:
            return {"status": "error", "reason": "These paths are not files.", "missing": missing}
        # Usability is checked before permission is asked, so a missing or
        # disabled input neither prompts nor leaves a one-shot UPLOAD grant live.
        target = page.locator(selector).first
        # Visibility is not required: a styled picker keeps its input hidden, and
        # a hidden input still accepts files (measured; see verify_forms_e2e.py).
        unusable = await _actionable(target, need_visible=False)
        if unusable:
            ctx.audit.record(
                "upload_file", {"selector": selector}, status=unusable, origin=origin.describe()
            )
            return {"status": unusable, "selector": selector, "url": page.url}
        denied, bought = await require(
            ctx,
            "upload_file",
            target=origin,
            capabilities=_scopes(Capability.UPLOAD),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            ctx.audit.record(
                "upload_file",
                {"selector": selector, "files": [str(p) for p in wanted]},
                status="needs_approval",
                origin=origin.describe(),
            )
            return denied
        async with released(ctx, bought):
            await target.set_input_files([str(p) for p in wanted])
            ctx.audit.record(
                "upload_file",
                {"selector": selector, "files": [str(p) for p in wanted]},
                origin=origin.describe(),
            )
            return {
                "status": "ok",
                "url": page.url,
                "files": [p.name for p in wanted],
                "count": len(wanted),
            }

    @mcp.tool
    async def read_draft(max_chars: int = 400) -> dict:
        """Collect what a form is about to publish, for a human to approve.

        Approving an item means reading the whole of it, and on a real form that
        means looking in several places at once: the visible fields, the rich-text
        body (a separate document ``read_page`` never shows), the attachment slots,
        and whether the item is currently a draft or already public. This gathers
        all of it into one answer so the approval request can carry the actual
        content rather than a description of it.

        Nothing is changed or sent — this is a read, so it needs no approval.
        Fields carry ``usable``; one that is false is hidden or disabled in the
        page right now and must be revealed before it can be set.
        """
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        cap = max(80, min(max_chars, 4000))
        dump = await page.evaluate(_DRAFT_JS, {"limit": cap})
        ctx.audit.record(
            "read_draft",
            {"fields": dump.get("fieldCount"), "editors": len(dump.get("editors", []))},
        )
        return {"status": "ok", "url": page.url, "title": await page.title(), **dump}

    @mcp.tool
    async def save_draft(submit: str = "", reason: str = "", confirm: bool = False) -> dict:
        """Save the item without showing it to anyone — the safe way to send a form.

        A site with no separate "save draft" button means every form POST is the
        one that could publish, so the difference has to be read off the page
        rather than chosen by the caller. This sends the form only after
        confirming the publish control is still on its private setting, and
        refuses if it is not — so it cannot be used to publish, and a page left
        armed by anything else will not slip out through it.

        Measured on KVR: a new news item opens with ``is_draft=1`` and posting the
        form then saves it privately.

        ``submit`` names the control that sends the form — ``read_form`` reports it
        in ``submitText`` (``Submit`` on a news item, ``Add`` on a product). This
        asks for ``SUBMIT`` but never for ``PUBLISH``: saving is an ordinary
        request, and keeping the two apart is what lets an operator write and keep
        an item without ever approving a release.

        Use ``publish`` instead when the item should go out to an audience.
        """
        if not submit.strip():
            return {
                "status": "needs_submit",
                "error": "save_draft needs the control that sends the form. Pass "
                "submit=<the control from read_form's submitText>.",
            }
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        sender = page.locator(submit).first
        sendable = await _actionable(sender)
        if sendable:
            ctx.audit.record(
                "save_draft", {"submit": submit}, status=sendable, origin=origin.describe()
            )
            return {"status": sendable, "selector": submit, "url": page.url}
        # Read the page's own state before sending. The caller does not choose
        # this — a form that is already armed to publish must not be sent by a
        # tool that claims to save privately.
        state = await page.evaluate(_PUBLISH_STATE_JS)
        # Every checked member must read as private. A group nobody can classify
        # is not a safe one, so an unrecognised control refuses rather than sends.
        if not state or not all(_reads_as_private(s) for s in state):
            ctx.audit.record(
                "save_draft",
                {"submit": submit, "state": state},
                status="not_private",
                origin=origin.describe(),
            )
            return {
                "status": "not_private",
                "error": "the publish control does not read as private, so sending "
                "this form could make the item public. Set it back to its "
                "draft setting (read_form's drafts list) or use publish "
                "with the operator's approval.",
                "state": state,
                "url": page.url,
            }
        denied, bought = await require(
            ctx,
            "save_draft",
            target=origin,
            capabilities=[Capability.INTERACT, Capability.SUBMIT],
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            ctx.audit.record(
                "save_draft", {"submit": submit}, status="needs_approval", origin=origin.describe()
            )
            return denied
        async with released(ctx, bought):
            # A refusal reports itself in a dialog. The session's handler answers it
            # — a dialog nobody answers freezes the page — and this window collects
            # what it said, from before the send until the page has been read back.
            with ctx.session.dialogs.watch(page) as dialogs:
                await sender.click()
                # A click succeeding means the element was clicked, not that the server
                # took the form. Measured on KVR: a rejected save and an accepted one
                # both left the page on the same URL, so the page has to be read back.
                after = await _await_settle(page, dialogs)
            ctx.audit.record(
                "save_draft",
                {
                    "submit": submit,
                    "reason": reason,
                    "state": state,
                    "refused": bool(after.get("refused")),
                },
                origin=origin.describe(),
            )
            # A refusal is reported as such: the caller must not read "ok" as
            # "saved" when the server answered with reasons instead.
            return {
                "status": after.pop("status", "ok"),
                "url": page.url,
                "state": state,
                "published": False,
                **after,
            }

    @mcp.tool
    async def publish(
        selector: str, submit: str = "", reason: str = "", confirm: bool = False
    ) -> dict:
        """Make a draft public — the one action that sends content to an audience.

        Kept apart from every other tool so that writing a draft can never publish
        it: this asks for ``PUBLISH``, which is single-use and implied by nothing,
        and it is the only tool that buys it.

        A site splits this across two controls and means neither of them alone.
        Measured on KVR: the publish radio (``input[name=is_draft][value='0']`` on
        a news item, ``input[name=is_live][value='1']`` on a product) only arms the
        form — the item travels on the form's own Submit. Switching the radio and
        stopping would leave the page primed to publish on someone else's next
        click, which is worse than not switching it at all. So give both:

        * ``selector`` — the control that carries the item live, from
          ``read_form``'s ``drafts`` list.
        * ``submit`` — the control that sends the form. **Required**: ``read_form``
          reports the candidates in ``submitText``, and on KVR they are one button
          (``Submit`` on a news item, ``Add`` on a product). This is the request
          that actually publicises the item, so nothing else in this tool surface
          may send a form whose publish control is set.

        Call this only when a human has approved the exact item. Show them what
        will go public and get their answer first; the approval prompt this raises
        is the second gate, not the first. Afterwards ``read_form`` reads back the
        state the page actually reached.
        """
        if not submit.strip():
            return {
                "status": "needs_submit",
                "error": "publish needs both controls: the one that makes the item "
                "live and the one that sends the form. Setting the live "
                "control alone leaves the form armed to publish on a later "
                "click. Pass submit=<the control from read_form's "
                "submitText>.",
                "selector": selector,
            }
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        # Whether the controls can be used is read before permission is asked:
        # a one-shot grant bought for a control that turns out to be hidden would
        # outlive this call, and nobody should be prompted for what cannot run.
        target = page.locator(selector).first
        unusable = await _actionable(target)
        if unusable:
            ctx.audit.record(
                "publish", {"selector": selector}, status=unusable, origin=origin.describe()
            )
            return {"status": unusable, "selector": selector, "url": page.url}
        sender = page.locator(submit).first
        sendable = await _actionable(sender)
        if sendable:
            ctx.audit.record(
                "publish", {"selector": submit}, status=sendable, origin=origin.describe()
            )
            return {"status": sendable, "selector": submit, "url": page.url}
        denied, bought = await require(
            ctx,
            "publish",
            target=origin,
            capabilities=[Capability.INTERACT, Capability.PUBLISH, Capability.SUBMIT],
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            ctx.audit.record(
                "publish", {"selector": selector}, status="needs_approval", origin=origin.describe()
            )
            return denied
        async with released(ctx, bought):
            await target.check()
            # The state is read before the send, so a server that reports a
            # validation failure does not look like a successful release.
            state = await page.evaluate(_PUBLISH_STATE_JS)
            with ctx.session.dialogs.watch(page) as dialogs:
                await sender.click()
                after = await _await_settle(page, dialogs)
            ctx.audit.record(
                "publish",
                {
                    "selector": selector,
                    "submit": submit,
                    "reason": reason,
                    "refused": bool(after.get("refused")),
                },
                origin=origin.describe(),
            )
            return {"status": after.pop("status", "ok"), "url": page.url, "state": state, **after}
