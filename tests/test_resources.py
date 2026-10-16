"""Where rwc finds runtime/, stdlib/, and scripts/ (#78)."""

from __future__ import annotations

import sys
from pathlib import Path

from rwc import resources
from rwc.loader import stdlib_dir

REPO = Path(__file__).resolve().parent.parent


def test_checkout_layout(monkeypatch):
    monkeypatch.delenv("RW_HOME", raising=False)
    monkeypatch.delenv("RW_STDLIB", raising=False)
    assert resources.rw_home() == REPO
    assert stdlib_dir() == REPO / "stdlib"


def test_rw_home_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("RW_HOME", str(tmp_path))
    monkeypatch.delenv("RW_STDLIB", raising=False)
    assert resources.rw_home() == tmp_path
    assert stdlib_dir() == tmp_path / "stdlib"


def test_frozen_bundle(monkeypatch, tmp_path):
    monkeypatch.delenv("RW_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.rw_home() == tmp_path
