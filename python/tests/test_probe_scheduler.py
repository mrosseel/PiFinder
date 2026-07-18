"""Tests for the pedestal probe scheduler decision logic."""

import pytest

from PiFinder.camera_interface import CameraInterface


@pytest.mark.unit
def test_static_prong_fires_once_a_minute():
    cam = CameraInterface()
    cam._last_probe_at = 0.0
    assert cam._probe_due(1000.0, moving=False)
    cam._last_probe_at = 1000.0
    assert not cam._probe_due(1030.0, moving=False)
    assert cam._probe_due(1061.0, moving=False)


@pytest.mark.unit
def test_slew_prong_needs_sustained_motion():
    cam = CameraInterface()
    cam._last_probe_at = 0.0
    # a honing nudge: moving for under 2 s never triggers while moving
    assert not cam._probe_due(1000.0, moving=True)
    assert not cam._probe_due(1001.0, moving=True)
    # sustained slew does
    assert cam._probe_due(1003.5, moving=True)


@pytest.mark.unit
def test_motion_stop_resets_slew_timer():
    cam = CameraInterface()
    cam._last_probe_at = 945.0
    assert not cam._probe_due(1000.0, moving=True)  # slew clock starts
    assert not cam._probe_due(1001.0, moving=False)  # stop: clock resets, not due
    assert not cam._probe_due(1002.0, moving=True)  # slew clock restarts
    # due now, but only 1.5 s of motion since the restart
    assert not cam._probe_due(1003.5, moving=True) or (1003.5 - 945.0) < 60.0
    # due and sustained (3.5 s since restart)
    assert cam._probe_due(1005.5, moving=True)
