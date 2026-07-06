"""pytest config: locate the shared spec/ fixtures and reset module state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# tests/ -> meter-py -> packages -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_DIR = REPO_ROOT / "spec"


@pytest.fixture
def example() -> dict:
    """The canonical record — the SAME fixture meter-ts validates against."""
    return json.loads((SPEC_DIR / "record.example.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _reset_meter_state():
    from meter.meter import _reset_warn_once

    _reset_warn_once()
    yield
