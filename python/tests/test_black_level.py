"""Tests for the local-pair black-level estimator."""

import numpy as np
import pytest

from PiFinder.sqm.black_level import BlackLevelTracker


def feed_sweep(tracker, pedestal=236.0, rate=300.0, t0=1000.0, cadence=2.5, n=20):
    """Sweep-shaped diet: randomized 25 ms - 1 s exposures, seconds apart."""
    rng = np.random.default_rng(42)
    exps = np.geomspace(0.025, 1.0, n)
    rng.shuffle(exps)
    for i, e in enumerate(exps):
        t = t0 + i * cadence
        tracker.add_sample(float(e), pedestal + rate * float(e), captured_at=t)
    return t0 + n * cadence


@pytest.mark.unit
def test_recovers_pedestal_from_sweep_diet():
    tracker = BlackLevelTracker(bias_offset=238.0)
    feed_sweep(tracker)
    assert tracker.pedestal() == pytest.approx(236.0, abs=0.3)
    assert tracker.stderr() < 1.0
    assert tracker.dump()["n_pairs"] >= 8


@pytest.mark.unit
def test_immune_to_smooth_sky_drift():
    """The 2026-07-17 failure: transmission drifting over minutes biased the
    global fit by 8 ADU at a passing stderr. Local pairs see the same sky on
    both members, so the estimate stays put while the sky drifts 30%."""
    tracker = BlackLevelTracker(bias_offset=238.0)
    rng = np.random.default_rng(7)
    exps = np.geomspace(0.025, 1.0, 20)
    rng.shuffle(exps)
    for i, e in enumerate(exps):
        t = 1000.0 + i * 2.5
        rate = 300.0 * (1.0 + 0.3 * i / 20)  # cloud thinning over the sweep
        tracker.add_sample(float(e), 236.0 + rate * float(e), captured_at=t)
    assert tracker.pedestal() == pytest.approx(236.0, abs=1.0)


@pytest.mark.unit
def test_erratic_sky_rejected_by_pair_spread():
    """Transmission jumping between pairs shows up as pair-intercept spread
    (unlike a global fit's stderr, which smooth drift can evade)."""
    tracker = BlackLevelTracker(bias_offset=238.0)
    rng = np.random.default_rng(3)
    exps = np.geomspace(0.025, 1.0, 20)
    rng.shuffle(exps)
    for i, e in enumerate(exps):
        t = 1000.0 + i * 2.5
        rate = 300.0 * (1.0 + 0.4 * rng.standard_normal())
        tracker.add_sample(float(e), 236.0 + rate * float(e), captured_at=t)
    assert tracker.pedestal() is None


@pytest.mark.unit
def test_pinned_exposure_yields_no_pairs():
    tracker = BlackLevelTracker(bias_offset=238.0)
    for i in range(60):
        tracker.add_sample(1.0, 536.0, captured_at=1000.0 + i)
    assert tracker.pedestal() is None
    assert tracker.dump()["n_pairs"] == 0


@pytest.mark.unit
def test_probe_scenario_anchors_plus_short_frames():
    """Production shape: a stream of 1 s frames plus one 3-frame probe
    excursion at 300 ms produces enough local pairs on its own."""
    tracker = BlackLevelTracker(bias_offset=238.0)
    for i in range(12):
        tracker.add_sample(1.0, 536.0, captured_at=1000.0 + i)
    for k in range(3):
        tracker.add_sample(0.3, 236.0 + 300.0 * 0.3, captured_at=1012.5 + k * 0.5)
    assert tracker.pedestal() == pytest.approx(236.0, abs=0.3)


@pytest.mark.unit
def test_pairs_respect_time_locality():
    """Samples further apart than pair_max_dt never pair — a short frame
    cannot pair against an anchor from the other side of a sky change."""
    tracker = BlackLevelTracker(bias_offset=238.0, min_pairs=1)
    tracker.add_sample(1.0, 536.0, captured_at=1000.0)
    tracker.add_sample(0.3, 326.0, captured_at=1100.0)  # 100 s later
    assert tracker.pedestal() is None
    tracker.add_sample(0.3, 326.0, captured_at=1105.0)  # pairs with neither 1 s
    assert tracker.pedestal() is None


@pytest.mark.unit
def test_rejects_estimate_far_from_profile():
    tracker = BlackLevelTracker(bias_offset=300.0)
    feed_sweep(tracker, pedestal=236.0)  # 64 ADU from the anchor
    assert tracker.pedestal() is None


@pytest.mark.unit
def test_lease_expires_without_fresh_acceptance(monkeypatch):
    fake_now = [5000.0]
    monkeypatch.setattr("PiFinder.sqm.black_level.time.monotonic", lambda: fake_now[0])
    tracker = BlackLevelTracker(bias_offset=238.0, max_age_seconds=900.0)
    feed_sweep(tracker)
    assert tracker.pedestal() is not None

    # A pinned hour turns the window over; nothing re-qualifies.
    for i in range(60):
        tracker.add_sample(1.0, 536.0, captured_at=2000.0 + i)
    fake_now[0] += 600.0
    assert tracker.pedestal() is not None  # within the lease
    fake_now[0] += 400.0
    assert tracker.pedestal() is None  # expired -> profile fallback


@pytest.mark.unit
def test_stable_gate_and_invalid_inputs_drop_samples():
    tracker = BlackLevelTracker(bias_offset=238.0)
    tracker.add_sample(1.0, 536.0, stable=False, captured_at=1000.0)
    tracker.add_sample(0.0, 536.0, captured_at=1000.0)
    tracker.add_sample(1.0, float("nan"), captured_at=1000.0)
    tracker.add_sample(None, 536.0, captured_at=1000.0)
    assert tracker.dump()["n_samples"] == 0


@pytest.mark.unit
def test_reset_clears_everything():
    tracker = BlackLevelTracker(bias_offset=238.0)
    feed_sweep(tracker)
    tracker.reset()
    assert tracker.pedestal() is None
    dump = tracker.dump()
    assert dump["n_samples"] == 0
    assert dump["age_seconds"] is None
