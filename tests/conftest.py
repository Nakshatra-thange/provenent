from __future__ import annotations

from pathlib import Path

import pytest

from provenant.demo import write_demo


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    """A fresh copy of the TE-2 demo project."""
    return write_demo(tmp_path / "te2")


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
