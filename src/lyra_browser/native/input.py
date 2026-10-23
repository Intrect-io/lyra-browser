"""``Keyboard`` and ``Mouse``: trusted input through ``Input.dispatch*Event``.

Events go to the renderer as if from the OS, so pages see ``isTrusted`` input and a
click lands where a person's would (the locator hit-tests first). Text that is not a
key on a US keyboard (Korean, emoji, any IME composition) goes through
``Input.insertText``, the path a committed IME composition takes.

On macOS, Chrome does not turn Meta+A/C/X/V/Z into editing commands for synthetic
events; they are sent with the matching ``commands`` the way Playwright sends them.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import Error

if TYPE_CHECKING:
    from .page import NativePage

_MODIFIER_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}


@dataclass(frozen=True)
class KeyDef:
    key: str
    code: str
    key_code: int
    text: str = ""
    shift_key: str = ""
    location: int = 0


def _build_layout() -> dict[str, KeyDef]:
    layout: dict[str, KeyDef] = {}
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
        upper = ch.upper()
        layout[ch] = KeyDef(ch, f"Key{upper}", 65 + i, ch, upper)
        layout[upper] = KeyDef(upper, f"Key{upper}", 65 + i, upper)
    shifted_digits = ")!@#$%^&*("
    for d in range(10):
        layout[str(d)] = KeyDef(str(d), f"Digit{d}", 48 + d, str(d), shifted_digits[d])
        layout[shifted_digits[d]] = KeyDef(
            shifted_digits[d], f"Digit{d}", 48 + d, shifted_digits[d]
        )
    punctuation = [
        ("-", "_", "Minus", 189),
        ("=", "+", "Equal", 187),
        ("[", "{", "BracketLeft", 219),
        ("]", "}", "BracketRight", 221),
        ("\\", "|", "Backslash", 220),
        (";", ":", "Semicolon", 186),
        ("'", '"', "Quote", 222),
        (",", "<", "Comma", 188),
        (".", ">", "Period", 190),
        ("/", "?", "Slash", 191),
        ("`", "~", "Backquote", 192),
    ]
    for plain, shifted, code, key_code in punctuation:
        layout[plain] = KeyDef(plain, code, key_code, plain, shifted)
        layout[shifted] = KeyDef(shifted, code, key_code, shifted)
    named = [
        KeyDef("Enter", "Enter", 13, "\r"),
        KeyDef("Tab", "Tab", 9),
        KeyDef("Backspace", "Backspace", 8),
        KeyDef("Delete", "Delete", 46),
        KeyDef("Escape", "Escape", 27),
        KeyDef(" ", "Space", 32, " "),
        KeyDef("ArrowUp", "ArrowUp", 38),
        KeyDef("ArrowDown", "ArrowDown", 40),
        KeyDef("ArrowLeft", "ArrowLeft", 37),
        KeyDef("ArrowRight", "ArrowRight", 39),
        KeyDef("Home", "Home", 36),
        KeyDef("End", "End", 35),
        KeyDef("PageUp", "PageUp", 33),
        KeyDef("PageDown", "PageDown", 34),
        KeyDef("Insert", "Insert", 45),
        KeyDef("Shift", "ShiftLeft", 16, location=1),
        KeyDef("Control", "ControlLeft", 17, location=1),
        KeyDef("Alt", "AltLeft", 18, location=1),
        KeyDef("Meta", "MetaLeft", 91, location=1),
    ]
    for kd in named:
        layout[kd.key] = kd
    layout["Space"] = layout[" "]
    layout["Esc"] = layout["Escape"]
    for n in range(1, 13):
        layout[f"F{n}"] = KeyDef(f"F{n}", f"F{n}", 111 + n)
    return layout


LAYOUT = _build_layout()
_MAC_COMMANDS = {
    ("Meta", "a"): "selectAll",
    ("Meta", "c"): "copy",
    ("Meta", "x"): "cut",
    ("Meta", "v"): "paste",
    ("Meta", "z"): "undo",
    ("Meta+Shift", "z"): "redo",
}


def split_combo(combo: str) -> tuple[list[str], str]:
    """``"Control+Shift+a"`` -> (["Control", "Shift"], "a"). A lone ``+`` is the key."""
    if combo == "+":
        return [], "+"
    if combo.endswith("++"):
        mods, key = combo[:-2].split("+"), "+"
    else:
        *mods, key = combo.split("+")
    resolved = []
    for mod in mods:
        if mod == "ControlOrMeta":
            mod = "Meta" if sys.platform == "darwin" else "Control"
        if mod not in _MODIFIER_BITS:
            raise Error(f'Keyboard.press: Unknown modifier "{mod}" in "{combo}"')
        resolved.append(mod)
    return resolved, key


class Keyboard:
    def __init__(self, page: NativePage) -> None:
        self._page = page
        self._held: set[str] = set()

    @property
    def modifiers(self) -> int:
        return sum(_MODIFIER_BITS[m] for m in self._held if m in _MODIFIER_BITS)

    async def _key_event(
        self, kind: str, kd: KeyDef, text: str = "", commands: list[str] | None = None
    ) -> None:
        params: dict = {
            "type": kind,
            "modifiers": self.modifiers,
            "key": kd.key,
            "code": kd.code,
            "windowsVirtualKeyCode": kd.key_code,
            "nativeVirtualKeyCode": kd.key_code,
            "location": kd.location,
            "autoRepeat": False,
            "isKeypad": False,
        }
        if text:
            params["text"] = text
            params["unmodifiedText"] = text
        if commands:
            params["commands"] = commands
        await self._page._send("Input.dispatchKeyEvent", params)

    def _definition(self, key: str) -> KeyDef | None:
        kd = LAYOUT.get(key)
        if kd is not None and "Shift" in self._held and kd.shift_key:
            shifted = LAYOUT.get(kd.shift_key)
            return KeyDef(
                kd.shift_key, kd.code, kd.key_code, shifted.text if shifted else kd.shift_key
            )
        return kd

    async def down(self, key: str) -> None:
        kd = self._definition(key)
        if kd is None:
            raise Error(f'Keyboard.down: Unknown key: "{key}"')
        if kd.key in _MODIFIER_BITS:
            self._held.add(kd.key)
        # A printable key types only when no command modifier is held (Control+a selects).
        typing = kd.text and not (self._held & {"Control", "Meta", "Alt"})
        others = self._held - {kd.key}
        commands = None
        if sys.platform == "darwin" and "Meta" in others and others <= {"Meta", "Shift"}:
            combo = "Meta+Shift" if "Shift" in others else "Meta"
            command = _MAC_COMMANDS.get((combo, kd.key.lower()))
            commands = [command] if command else None
        await self._key_event(
            "keyDown" if typing else "rawKeyDown", kd, kd.text if typing else "", commands
        )

    async def up(self, key: str) -> None:
        kd = self._definition(key)
        if kd is None:
            raise Error(f'Keyboard.up: Unknown key: "{key}"')
        self._held.discard(kd.key)
        await self._key_event("keyUp", kd)

    async def press(self, key: str, *, delay: float = 0) -> None:
        mods, main = split_combo(key)
        if main not in LAYOUT and len(main) == 1 and not mods:
            await self.insert_text(main)
            return
        if main not in LAYOUT:
            raise Error(f'Keyboard.press: Unknown key: "{main}"')
        for mod in mods:
            await self.down(mod)
        try:
            await self.down(main)
            if delay:
                await asyncio.sleep(delay / 1000)
            await self.up(main)
        finally:
            for mod in reversed(mods):
                await self.up(mod)

    async def insert_text(self, text: str) -> None:
        await self._page._send("Input.insertText", {"text": text})

    async def type(self, text: str, *, delay: float = 0) -> None:
        for ch in text:
            if ch in LAYOUT and LAYOUT[ch].text:
                await self.press(ch, delay=delay)
            else:
                await self.insert_text(ch)
            if delay:
                await asyncio.sleep(delay / 1000)


class Mouse:
    def __init__(self, page: NativePage, keyboard: Keyboard) -> None:
        self._page = page
        self._keyboard = keyboard
        self.x = 0.0
        self.y = 0.0
        self._buttons: set[str] = set()

    def _button_bits(self) -> int:
        return sum({"left": 1, "right": 2, "middle": 4}[b] for b in self._buttons)

    async def _event(self, kind: str, **extra) -> None:
        params = {
            "type": kind,
            "x": self.x,
            "y": self.y,
            "modifiers": self._keyboard.modifiers,
            "buttons": self._button_bits(),
            **extra,
        }
        await self._page._send("Input.dispatchMouseEvent", params)

    async def move(self, x: float, y: float, *, steps: int = 1) -> None:
        start_x, start_y = self.x, self.y
        steps = max(1, int(steps))
        for i in range(1, steps + 1):
            self.x = start_x + (x - start_x) * i / steps
            self.y = start_y + (y - start_y) * i / steps
            pressed = next(iter(self._buttons), "none")
            await self._event("mouseMoved", button=pressed)

    async def down(self, *, button: str = "left", click_count: int = 1) -> None:
        self._buttons.add(button)
        await self._event("mousePressed", button=button, clickCount=click_count)

    async def up(self, *, button: str = "left", click_count: int = 1) -> None:
        self._buttons.discard(button)
        await self._event("mouseReleased", button=button, clickCount=click_count)

    async def click(
        self, x: float, y: float, *, button: str = "left", click_count: int = 1, delay: float = 0
    ) -> None:
        await self.move(x, y)
        for n in range(1, click_count + 1):
            await self.down(button=button, click_count=n)
            if delay:
                await asyncio.sleep(delay / 1000)
            await self.up(button=button, click_count=n)

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        await self._event("mouseWheel", deltaX=delta_x, deltaY=delta_y, button="none")
