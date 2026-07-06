import time

import pytest

import meter.meter as _meter_module
from meter import _reset_warn_once


@pytest.fixture(autouse=True)
def _reset_state():
    _reset_warn_once()
    yield


def wait_until_warned(timeout: float = 3.0) -> None:
    """Block until the module-level warn-once flag fires (dispatch runs on a thread)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _meter_module._warned:
            time.sleep(0.02)  # let the stderr print flush after the flag flips
            return
        time.sleep(0.005)
    raise AssertionError("warn-once never fired")
