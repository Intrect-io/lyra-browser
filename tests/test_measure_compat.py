"""The compat measurement's arms: which backend and settings each cell launches with.

The live run (``scripts/measure_compat.py``) is a report, not a gate; this pins that the
cursor cells differ only in the cursor, so a gap between them is the cursor's.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import measure_compat  # noqa: E402


def test_the_cursor_cells_differ_only_in_the_cursor_and_both_hover():
    on_backend, on, on_hover = measure_compat.arm("cursor-on")
    off_backend, off, off_hover = measure_compat.arm("cursor-off")
    assert on_backend == off_backend == "cdp"
    assert on_hover and off_hover
    assert on.pop("cursor") is True and off.pop("cursor") is False
    assert on == off


@pytest.mark.parametrize(("cell", "backend"), [("cdp", "cdp"), ("route-master", "route")])
def test_the_guard_cells_are_unchanged(cell, backend):
    assert measure_compat.arm(cell) == (backend, {}, False)


def test_every_cell_has_an_arm_and_nothing_else_does():
    for cell in measure_compat.CELLS:
        measure_compat.arm(cell)
    with pytest.raises(KeyError):
        measure_compat.arm("cursor")
