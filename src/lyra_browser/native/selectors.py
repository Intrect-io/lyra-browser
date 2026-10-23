"""Playwright selector syntax, the part of it lyra and its callers use, parsed into steps.

A selector is ``>>``-separated parts, each run against what the part before it matched:

- ``css`` (the default): open shadow roots are pierced, as Playwright's css engine does.
  Playwright's ``:visible`` and ``:has-text("…")`` extensions become their own steps;
- ``text=``: ``text=Log in`` matches case-insensitively as a substring, ``text="Log in"``
  exactly, ``text=/log\\s*in/i`` by regular expression; the innermost elements win;
- ``role=button[name="Save"]``: resolved through Chrome's own accessibility tree
  (``Accessibility.queryAXTree``), so roles and names are the browser's;
- ``xpath=`` (or a part starting with ``//``), ``id=``, ``data-testid=``;
- ``nth=N`` (``-1`` is the last), ``visible=true|false``;
- ``aria-ref=e12`` / ``aria-ref=f1e3``: a ref from the last ``read_page(mode="tree")``.

Anything else is refused with an error naming the engine, rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import Error

ENGINES = frozenset(
    {"css", "text", "role", "xpath", "nth", "aria-ref", "id", "data-testid", "visible"}
)
_ENGINE_PREFIX = re.compile(r"^([a-zA-Z][\w-]*)=(.*)$", re.DOTALL)
_HAS_TEXT = re.compile(r""":has-text\(\s*(["'])(.*?)\1\s*\)""", re.DOTALL)
_VISIBLE = re.compile(r":visible\b")
_ROLE = re.compile(r"^\s*([a-zA-Z][\w-]*)\s*((?:\[[^\]]*\]\s*)*)$", re.DOTALL)
_ROLE_ATTR = re.compile(r"""\[\s*([\w-]+)\s*(?:=\s*(.*?))?\s*\]""", re.DOTALL)
_ROLE_STATES = frozenset({"checked", "disabled", "expanded", "pressed", "selected", "level"})


@dataclass(frozen=True)
class TextMatch:
    """How a text or a name is matched: ``substring`` (case-insensitive), ``exact``, ``regex``."""

    mode: str
    text: str
    flags: str = ""

    def as_js(self) -> dict:
        return {"mode": self.mode, "text": self.text, "flags": self.flags}

    def matches(self, value: str) -> bool:
        norm = " ".join((value or "").split())
        if self.mode == "exact":
            return norm == self.text
        if self.mode == "regex":
            flags = re.IGNORECASE if "i" in self.flags else 0
            return re.search(self.text, norm, flags) is not None
        return self.text.lower() in norm.lower()


@dataclass(frozen=True)
class Part:
    engine: str
    body: str = ""
    text: TextMatch | None = None
    role: str = ""
    name: TextMatch | None = None
    states: tuple[tuple[str, str], ...] = field(default=())
    include_hidden: bool = False

    def as_js(self) -> dict:
        return {
            "engine": self.engine,
            "body": self.body,
            "text": self.text.as_js() if self.text else None,
        }


def _split(selector: str) -> list[str]:
    """``>>``-separated parts, ignoring ``>>`` inside quotes, brackets and parentheses."""
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    i = 0
    while i < len(selector):
        ch = selector[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth = max(0, depth - 1)
        elif selector.startswith(">>", i) and depth == 0:
            parts.append(selector[start:i])
            i += 2
            start = i
            continue
        i += 1
    parts.append(selector[start:])
    return [p.strip() for p in parts]


def _unquote(value: str) -> tuple[str, str] | None:
    """``("text", flag)`` from ``"text"``, ``"text"i`` or ``'text's``; None if unquoted."""
    value = value.strip()
    match = re.fullmatch(r"""(["'])(.*)\1([is]?)""", value, re.DOTALL)
    if not match:
        return None
    return re.sub(r"\\(.)", r"\1", match.group(2)), match.group(3)


def text_match(raw: str) -> TextMatch:
    raw = raw.strip()
    regex = re.fullmatch(r"/(.*)/([a-z]*)", raw, re.DOTALL)
    if regex:
        return TextMatch("regex", regex.group(1), regex.group(2))
    quoted = _unquote(raw)
    if quoted is not None:
        text, flag = quoted
        return TextMatch("substring" if flag == "i" else "exact", " ".join(text.split()))
    return TextMatch("substring", " ".join(raw.split()))


def _role_name(raw: str) -> TextMatch:
    """A role selector's name: case-insensitive substring unless written ``"…"s`` (exact)."""
    match = text_match(raw)
    quoted = _unquote(raw)
    if match.mode == "exact" and quoted is not None and quoted[1] != "s":
        return TextMatch("substring", match.text)
    return match


def _role_part(body: str, selector: str) -> Part:
    match = _ROLE.fullmatch(body)
    if not match:
        raise Error(f'Error while parsing selector "{selector}": invalid role selector "{body}"')
    name: TextMatch | None = None
    states: list[tuple[str, str]] = []
    include_hidden = False
    for attr, value in _ROLE_ATTR.findall(match.group(2)):
        attr = attr.lower()
        if attr == "name":
            name = _role_name(value) if value else None
        elif attr == "include-hidden":
            include_hidden = value.strip().lower() not in ("false",)
        elif attr in _ROLE_STATES:
            states.append((attr, (value or "true").strip().strip("\"'").lower()))
        else:
            raise Error(
                f'Error while parsing selector "{selector}": unknown role attribute "{attr}"'
            )
    return Part(
        "role",
        role=match.group(1).lower(),
        name=name,
        states=tuple(states),
        include_hidden=include_hidden,
    )


def _css_parts(body: str) -> list[Part]:
    """A css part, with Playwright's ``:visible`` / ``:has-text()`` peeled into steps."""
    extra: list[Part] = []
    for _quote, text in _HAS_TEXT.findall(body):
        extra.append(Part("has-text", text=TextMatch("substring", " ".join(text.split()))))
    body = _HAS_TEXT.sub("", body)
    if _VISIBLE.search(body):
        body = _VISIBLE.sub("", body)
        extra.append(Part("visible", "true"))
    body = body.strip() or "*"
    return [Part("css", body), *extra]


def parse(selector: str) -> list[Part]:
    """The steps of ``selector``. Raises ``Error`` (Playwright's wording) when unreadable."""
    if not selector or not selector.strip():
        raise Error("Error while parsing selector: selector is empty")
    steps: list[Part] = []
    for chunk in _split(selector):
        if not chunk:
            raise Error(f'Error while parsing selector "{selector}": empty part')
        prefixed = _ENGINE_PREFIX.match(chunk)
        if prefixed:
            engine, body = prefixed.group(1).lower(), prefixed.group(2)
            if engine not in ENGINES:
                raise Error(f'Unknown engine "{engine}" while parsing selector {selector}')
            if engine == "css":
                steps.extend(_css_parts(body))
            elif engine == "text":
                steps.append(Part("text", text=text_match(body)))
            elif engine == "role":
                steps.append(_role_part(body, selector))
            elif engine == "xpath":
                steps.append(Part("xpath", body.strip()))
            elif engine == "nth":
                try:
                    steps.append(Part("nth", str(int(body.strip()))))
                except ValueError:
                    raise Error(f'Error while parsing selector "{selector}": nth={body}') from None
            elif engine == "visible":
                steps.append(Part("visible", "false" if body.strip() == "false" else "true"))
            elif engine == "id":
                steps.append(Part("css", f'[id="{_css_escape(body.strip())}"]'))
            elif engine == "data-testid":
                steps.append(Part("css", f'[data-testid="{_css_escape(body.strip())}"]'))
            else:  # aria-ref
                steps.append(Part("aria-ref", body.strip()))
        elif chunk.startswith(("//", "..")):
            steps.append(Part("xpath", chunk))
        elif _unquote(chunk) is not None:
            steps.append(Part("text", text=text_match(chunk)))
        else:
            steps.extend(_css_parts(chunk))
    return steps


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def describe(selector: str) -> str:
    """How a call log names a locator: ``locator('button.save')``."""
    return f"locator('{selector}')"
