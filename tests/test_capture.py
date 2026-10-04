"""Captures are files: written atomically, sized from the header, pruned oldest-first."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from conftest import FAKE_PNG
from lyra_browser.capture import envelope, png_size, save_capture
from lyra_browser.config import Config


def _cfg(tmp_path: Path, keep: int = 200) -> Config:
    cfg = Config(capture_keep=keep)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    cfg.capture_dir = tmp_path / "captures"
    return cfg


def test_png_size_reads_ihdr() -> None:
    assert png_size(FAKE_PNG) == (2, 3)


@pytest.mark.parametrize(
    "junk", [b"", b"not a png", FAKE_PNG[:20], b"\x89PNG\r\n\x1a\n" + b"x" * 16]
)
def test_png_size_rejects_non_png(junk: bytes) -> None:
    assert png_size(junk) is None


def test_save_capture_writes_the_file_and_no_partial(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    path = save_capture(cfg, FAKE_PNG, kind="page")
    assert path.parent == cfg.capture_dir
    assert path.name.startswith("page-") and path.suffix == ".png"
    assert path.read_bytes() == FAKE_PNG
    assert not list(cfg.capture_dir.glob("*.part"))


def test_save_capture_names_sort_by_time(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    first = save_capture(cfg, FAKE_PNG, kind="page")
    second = save_capture(cfg, FAKE_PNG, kind="element")
    assert sorted(p.name for p in cfg.capture_dir.glob("*.png")) == sorted(
        [first.name, second.name]
    )
    # Timestamp leads after the kind, so a listing reads as a log.
    assert first.name.split("-")[1] <= second.name.split("-")[1]


def test_prune_keeps_only_the_newest(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, keep=3)
    paths = [save_capture(cfg, FAKE_PNG, kind="page") for _ in range(5)]
    remaining = sorted(cfg.capture_dir.glob("*.png"))
    assert len(remaining) == 3
    assert all(p.exists() for p in paths[-3:])
    assert not any(p.exists() for p in paths[:2])


def test_prune_disabled_with_zero(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, keep=0)
    for _ in range(4):
        save_capture(cfg, FAKE_PNG, kind="page")
    assert len(list(cfg.capture_dir.glob("*.png"))) == 4


def test_envelope_is_a_path_by_default(tmp_path: Path) -> None:
    result = envelope(tmp_path / "x.png", FAKE_PNG, url="https://a.example/", inline=False)
    assert result["status"] == "ok"
    assert result["image_path"] == str(tmp_path / "x.png")
    assert result["mime_type"] == "image/png"
    assert (result["width"], result["height"]) == (2, 3)
    assert result["bytes"] == len(FAKE_PNG)
    assert "base64" not in result


def test_envelope_inline_adds_base64(tmp_path: Path) -> None:
    result = envelope(tmp_path / "x.png", FAKE_PNG, url="u", inline=True)
    assert base64.b64decode(result["base64"]) == FAKE_PNG
