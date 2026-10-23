"""The scripts the native locator runs in a frame's isolated world.

Each is a function called with ``Runtime.callFunctionOn``; none installs anything on the
page or reads a page global, so the page cannot see them or change what they compute.
Element states follow Playwright's definitions closely enough for the tools' checks:

- visible: connected, ``visibility: visible``, and a non-empty bounding box (an element
  with ``display: contents`` is visible when a child is);
- enabled: not natively disabled (a disabled ``fieldset`` disables its controls, except
  inside its first ``legend``) and not ``aria-disabled="true"`` on itself or an ancestor;
- editable: enabled and neither ``readonly`` nor ``aria-readonly="true"``.
"""

from __future__ import annotations

_HELPERS = r"""
  const parentOf = (n) => n.parentNode || (n instanceof ShadowRoot ? n.host : null);
  const describe = (e) => {
    if (!e || e.nodeType !== 1) return String(e);
    const tag = e.nodeName.toLowerCase();
    let attrs = '';
    for (const a of ['id', 'class', 'type', 'name', 'role', 'aria-label', 'href']) {
      const v = e.getAttribute(a);
      if (v) attrs += ` ${a}="${v.length > 40 ? v.slice(0, 40) + '…' : v}"`;
    }
    const text = (e.textContent || '').replace(/\s+/g, ' ').trim();
    const shown = text.length > 30 ? text.slice(0, 30) + '…' : text;
    return `<${tag}${attrs}>${shown}</${tag}>`;
  };
  const isVisible = (e) => {
    if (!e || !e.isConnected || e.nodeType !== 1) return false;
    const style = e.ownerDocument.defaultView.getComputedStyle(e);
    if (style.display === 'contents') {
      for (let c = e.firstChild; c; c = c.nextSibling) {
        if (c.nodeType === 1 && isVisible(c)) return true;
        if (c.nodeType === 3) {
          const r = e.ownerDocument.createRange();
          r.selectNodeContents(c);
          const b = r.getBoundingClientRect();
          if (b.width > 0 && b.height > 0) return true;
        }
      }
      return false;
    }
    if (style.visibility !== 'visible') return false;
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const NATIVE_DISABLE = new Set(['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'OPTION', 'OPTGROUP']);
  const isEnabled = (e) => {
    if (NATIVE_DISABLE.has(e.nodeName) && e.disabled) return false;
    if (e.nodeName === 'OPTION' || e.nodeName === 'OPTGROUP') {
      const group = e.closest('optgroup, select');
      if (group && group !== e && group.disabled) return false;
    }
    for (let n = e; n; n = parentOf(n)) {
      if (n.nodeType !== 1) continue;
      if (n.getAttribute('aria-disabled') === 'true') return false;
      if (n !== e && n.nodeName === 'FIELDSET' && n.disabled && NATIVE_DISABLE.has(e.nodeName)) {
        const legend = n.querySelector(':scope > legend');
        if (!legend || !legend.contains(e)) return false;
      }
    }
    return true;
  };
  const isEditable = (e) => {
    if (!isEnabled(e)) return false;
    if ((e.nodeName === 'INPUT' || e.nodeName === 'TEXTAREA') && e.readOnly) return false;
    for (let n = e; n; n = parentOf(n)) {
      if (n.nodeType === 1 && n.getAttribute('aria-readonly') === 'true') return false;
    }
    return true;
  };
"""

# One selector step. ``part`` is ``selectors.Part.as_js()``; the remaining arguments are
# the scope elements (none: the document). Returns the matched elements as an array.
STEP_JS = (
    r"""function (part, ...roots) {
"""
    + _HELPERS
    + r"""
  const scopes = roots.length ? roots : [document];
  const out = [];
  const seen = new Set();
  const add = (e) => { if (e && !seen.has(e)) { seen.add(e); out.push(e); } };
  // Every element under ``root``, open shadow roots included, in document order.
  const deep = (root) => {
    const found = [];
    const visit = (node) => {
      for (let c = node.firstElementChild; c; c = c.nextElementSibling) {
        found.push(c);
        if (c.shadowRoot) visit(c.shadowRoot);
        visit(c);
      }
    };
    if (root.nodeType === 1 && root.shadowRoot) visit(root.shadowRoot);
    visit(root);
    return found;
  };
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const textOf = (e) => {
    const type = (e.type || '').toLowerCase();
    const isButton = e.nodeName === 'INPUT' && ['button', 'submit', 'reset'].includes(type);
    return isButton ? norm(e.value) : norm(e.textContent);
  };
  const matcher = (t) => {
    if (t.mode === 'regex') { const re = new RegExp(t.text, t.flags); return (s) => re.test(s); }
    if (t.mode === 'exact') return (s) => s === t.text;
    const lower = t.text.toLowerCase();
    return (s) => s.toLowerCase().includes(lower);
  };
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'TITLE']);
  if (part.engine === 'css') {
    for (const s of scopes) for (const e of deep(s)) if (e.matches(part.body)) add(e);
  } else if (part.engine === 'xpath') {
    for (const s of scopes) {
      const doc = s.nodeType === 9 ? s : s.ownerDocument;
      const res = doc.evaluate(part.body, s, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      for (let i = 0; i < res.snapshotLength; i++) {
        const n = res.snapshotItem(i);
        if (n.nodeType === 1) add(n);
      }
    }
  } else if (part.engine === 'text') {
    const ok = matcher(part.text);
    for (const s of scopes) {
      const all = deep(s).filter((e) => !SKIP.has(e.nodeName) && ok(textOf(e)));
      const hits = new Set(all);
      for (const e of all) {
        let inner = false;
        for (let c = e.firstElementChild; c && !inner; c = c.nextElementSibling) {
          inner = hits.has(c);
        }
        if (!inner) add(e);
      }
    }
  } else if (part.engine === 'has-text') {
    const ok = matcher(part.text);
    for (const s of scopes) if (s.nodeType === 1 && ok(textOf(s))) add(s);
  } else if (part.engine === 'visible') {
    const want = part.body !== 'false';
    for (const s of scopes) if (s.nodeType === 1 && isVisible(s) === want) add(s);
  } else {
    throw new Error('unsupported selector engine ' + part.engine);
  }
  return out;
}"""
)

# The state the actionability checks read, in one round trip.
STATE_JS = (
    r"""function () {
"""
    + _HELPERS
    + r"""
  const e = this;
  const r = e.getBoundingClientRect();
  const type = (e.type || '').toLowerCase();
  const role = e.getAttribute('role') || '';
  return {
    connected: e.isConnected,
    visible: isVisible(e),
    enabled: e.isConnected && isEnabled(e),
    editable: e.isConnected && isEditable(e),
    checked: (e.nodeName === 'INPUT' && ['checkbox', 'radio'].includes(type))
      ? !!e.checked
      : (['checkbox', 'radio', 'switch', 'menuitemcheckbox'].includes(role)
          ? e.getAttribute('aria-checked') === 'true' : null),
    rect: [r.x, r.y, r.width, r.height],
    html: describe(e),
  };
}"""
)

# Whether the element's box stays put across two animation frames. Resolves false if
# the frames do not come within ``budget`` ms (a background tab may not paint).
STABLE_JS = r"""function (budget) {
  const e = this;
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), budget);
    requestAnimationFrame(() => {
      const a = e.getBoundingClientRect();
      requestAnimationFrame(() => {
        const b = e.getBoundingClientRect();
        clearTimeout(timer);
        resolve(a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height);
      });
    });
  });
}"""

# What a click at the element's own centre would hit: "ok", or the interceptor.
HIT_JS = (
    r"""function () {
"""
    + _HELPERS
    + r"""
  const e = this;
  const r = e.getBoundingClientRect();
  const x = r.x + r.width / 2, y = r.y + r.height / 2;
  let hit = e.ownerDocument.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit) return {ok: false, by: null, outside: true};
  for (let n = hit; n; n = parentOf(n)) if (n === e) return {ok: true};
  return {ok: false, by: describe(hit)};
}"""
)

# Focus and select an input's content before typing, or say why it cannot be filled.
FILL_JS = r"""function () {
  const e = this;
  const SET = new Set(['color', 'date', 'time', 'datetime-local', 'month', 'range', 'week']);
  const REFUSE = new Set(
    ['checkbox', 'radio', 'file', 'button', 'submit', 'reset', 'image', 'hidden']);
  if (e.nodeName === 'INPUT') {
    const type = (e.type || 'text').toLowerCase();
    if (REFUSE.has(type)) return {error: `Input of type "${type}" cannot be filled`};
    if (SET.has(type)) return {set: true};
    e.focus();
    try { e.select(); } catch (err) { /* select() is not offered by every input type */ }
    return {type: true};
  }
  if (e.nodeName === 'TEXTAREA') { e.focus(); e.select(); return {type: true}; }
  if (e.isContentEditable) {
    e.focus();
    const doc = e.ownerDocument;
    const range = doc.createRange();
    range.selectNodeContents(e);
    const sel = doc.defaultView.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return {type: true};
  }
  return {error: 'Element is not an <input>, <textarea> or [contenteditable] element'};
}"""

# A value for an input that is set rather than typed (date, color, range…).
SET_VALUE_JS = r"""function (value) {
  const e = this;
  e.focus();
  e.value = value;
  if (e.value !== value) throw new Error('Malformed value');
  e.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  e.dispatchEvent(new Event('change', {bubbles: true}));
}"""

SELECT_JS = r"""function (want) {
  const e = this;
  if (e.nodeName !== 'SELECT') return {error: 'Element is not a <select> element'};
  const options = Array.from(e.options);
  let found;
  if ('value' in want) found = options.find((o) => o.value === want.value);
  else if ('label' in want)
    found = options.find((o) => o.label === want.label || o.textContent.trim() === want.label);
  else found = options[want.index];
  if (!found) return {missing: true};
  for (const o of options) o.selected = o === found;
  e.dispatchEvent(new Event('input', {bubbles: true, composed: true}));
  e.dispatchEvent(new Event('change', {bubbles: true}));
  return {values: [found.value]};
}"""

INPUT_VALUE_JS = r"""function () {
  const e = this;
  if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(e.nodeName))
    throw new Error('Node is not an <input>, <textarea> or <select> element');
  return e.value;
}"""

FILE_INPUT_JS = r"""function (count) {
  const e = this;
  if (e.nodeName !== 'INPUT' || (e.type || '').toLowerCase() !== 'file')
    return 'Node is not an HTMLInputElement of type "file"';
  if (count > 1 && !e.multiple) return 'Non-multiple file input can only accept single file';
  return '';
}"""

FOCUS_JS = r"""function () { this.focus(); }"""
INNER_TEXT_JS = r"""function () { return this.innerText; }"""
COUNT_JS = r"""function () { return this.length; }"""
