"""
Black-level (pedestal) tracking from local exposure pairs.

The sensor background is linear in exposure:

    background_per_pixel = pedestal + (dark_current + sky_rate) * exposure

so any two frames taken close together in time — under the same sky,
whatever that sky is doing on the minutes scale — determine the pedestal as
the intercept of the line through their (exposure, background) points. The
estimator collects such **local pairs** (close in time, well separated in
exposure) and publishes the median of their intercepts.

Local pairing is what makes the estimate weather-proof. A single global fit
over a minutes-long window blends samples taken under different transmissions
(cloud thickening, twilight) and the blend lands in the intercept as bias
that no residual-based quality gate can see — a smoothly drifting sky yields
small residuals around a *wrong* line (observed 2026-07-17: an 8 ADU bias
accepted at intercept stderr 0.97). Pair intercepts taken at different times
under a drifting sky instead *disagree with each other*, so the drift shows
up as spread among the estimates — the published uncertainty is honest by
construction. Validated on the 2026-07 sweep archive (29 sweeps, 2 sensors,
6 nights): pair medians stayed within ~1 ADU of the clear-sky ramp consensus
through cloud and drift that biased global fits by 2-8 ADU, and the pair
spread ranked sky quality correctly on every sweep.

Pairs need exposure contrast, which auto-exposure rarely provides — a pinned
1 s night yields no pairs at all, and the estimator honestly returns ``None``
(callers fall back to the profile constant). The camera process therefore
injects periodic short-exposure **probe** frames (``profile.probe_exposure_us``,
chosen on each sensor's linear branch); every frame's radiometer sample
carries its driver-reported exposure, so probe and transition frames enter
the window as ordinary valid samples.

An accepted estimate is a lease, not a latch: it expires ``max_age_seconds``
after the last accepted refit, so one plausible-but-wrong result can never
rule a session.
"""

import logging
import time
from collections import deque
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("SQM.BlackLevel")


class BlackLevelTracker:
    """Rolling pedestal estimate from local (time-adjacent) exposure pairs."""

    def __init__(
        self,
        bias_offset: float,
        max_samples: int = 60,
        pair_max_dt_seconds: float = 10.0,
        pair_min_exposure_ratio: float = 2.0,
        min_pairs: int = 8,
        max_pair_mad: float = 1.0,
        max_offset_deviation: float = 12.0,
        max_age_seconds: float = 900.0,
    ):
        """
        Args:
            bias_offset: profile pedestal (ADU); an estimate further than
                ``max_offset_deviation`` from it is rejected as pathological.
            max_samples: rolling window of (time, exposure, background) samples.
            pair_max_dt_seconds: two samples pair only when captured within
                this many seconds — the sky must not have had time to change
                between them.
            pair_min_exposure_ratio: paired exposures must differ by at least
                this factor; a small separation divides noise by a near-zero
                exposure difference.
            min_pairs: pair intercepts needed before an estimate is published.
            max_pair_mad: reject the estimate when the median absolute
                deviation of the pair intercepts exceeds this (ADU). Measured
                on the sweep archive: calm sky 0.3-0.9, drifting/patchy >1.2.
            max_offset_deviation: sanity band around the profile constant (ADU).
            max_age_seconds: an accepted estimate expires after this long
                without a fresh accepting refit; callers then fall back to
                the profile constant.
        """
        self.bias_offset = bias_offset
        self.pair_max_dt_seconds = pair_max_dt_seconds
        self.pair_min_exposure_ratio = pair_min_exposure_ratio
        self.min_pairs = min_pairs
        self.max_pair_mad = max_pair_mad
        self.max_offset_deviation = max_offset_deviation
        self.max_age_seconds = max_age_seconds
        self._samples: deque = deque(maxlen=max_samples)
        self._pedestal: Optional[float] = None
        self._pair_mad: Optional[float] = None
        self._n_pairs: int = 0
        self._accepted_at: Optional[float] = None

    def add_sample(
        self,
        exposure_sec: float,
        background_per_pixel: float,
        stable: bool = True,
        captured_at: Optional[float] = None,
    ) -> None:
        """Record one frame's raw (pre-pedestal) background and refit.

        Args:
            exposure_sec: driver-reported frame exposure (seconds). Requested
                exposures are not trustworthy: drivers deliver transitional
                frames at other-than-requested exposures.
            background_per_pixel: median sky background in ADU *before*
                pedestal subtraction.
            stable: False drops the sample (caller-side withhold). Local
                pairing already makes the fit itself insensitive to sky
                changes slower than ``pair_max_dt_seconds``.
            captured_at: sample capture epoch (seconds); defaults to now.
        """
        if (
            not stable
            or exposure_sec is None
            or exposure_sec <= 0
            or background_per_pixel is None
            or not np.isfinite(background_per_pixel)
        ):
            return
        t = float(captured_at) if captured_at is not None else time.time()
        self._samples.append((t, float(exposure_sec), float(background_per_pixel)))
        self._refit()

    def _pair_intercepts(self) -> np.ndarray:
        """Intercepts of every valid local pair in the window."""
        samples = sorted(self._samples)
        intercepts = []
        n = len(samples)
        for i in range(n):
            t1, e1, b1 = samples[i]
            for j in range(i + 1, n):
                t2, e2, b2 = samples[j]
                if t2 - t1 > self.pair_max_dt_seconds:
                    break
                lo, hi = (e1, e2) if e1 <= e2 else (e2, e1)
                if lo <= 0 or hi / lo < self.pair_min_exposure_ratio:
                    continue
                intercepts.append(b1 - e1 * (b2 - b1) / (e2 - e1))
        return np.array(intercepts)

    def _refit(self) -> None:
        intercepts = self._pair_intercepts()
        self._n_pairs = len(intercepts)
        if self._n_pairs < self.min_pairs:
            return  # keep the leased estimate until it expires
        pedestal = float(np.median(intercepts))
        pair_mad = float(np.median(np.abs(intercepts - pedestal)))
        if pair_mad > self.max_pair_mad:
            # Pair estimates disagree: the sky changed between pairs or the
            # short frames sit off the linear branch. Unlike a global-fit
            # stderr, this spread also exposes smooth drift.
            return
        if abs(pedestal - self.bias_offset) > self.max_offset_deviation:
            logger.debug(
                "Black-level pairs %.1f rejected: %.1f ADU from profile %.1f",
                pedestal,
                abs(pedestal - self.bias_offset),
                self.bias_offset,
            )
            return
        self._pedestal = pedestal
        self._pair_mad = pair_mad
        self._accepted_at = time.monotonic()

    def pedestal(self) -> Optional[float]:
        """Current pedestal (ADU), or None until/after a confident estimate.

        An accepted estimate is a lease: unless a fresh refit passes within
        ``max_age_seconds`` its evidence has aged out of the window
        unreplaced, and callers fall back to the profile constant — the same
        state every session starts in.
        """
        if self._pedestal is None or self._accepted_at is None:
            return None
        if time.monotonic() - self._accepted_at > self.max_age_seconds:
            return None
        return self._pedestal

    def stderr(self) -> Optional[float]:
        """Spread (MAD, ADU) of the accepted pair intercepts, or None."""
        return self._pair_mad

    def state(self) -> Tuple[Optional[float], Optional[float], int]:
        """(pedestal, pair MAD, n_samples) for diagnostics."""
        return self._pedestal, self._pair_mad, len(self._samples)

    def dump(self) -> dict:
        """Full JSON-serializable window state for diagnostics/sweeps."""
        return {
            "pedestal": self.pedestal(),
            "pair_mad": self._pair_mad,
            "n_pairs": self._n_pairs,
            "n_samples": len(self._samples),
            "age_seconds": (
                time.monotonic() - self._accepted_at
                if self._accepted_at is not None
                else None
            ),
            "config": {
                "bias_offset": self.bias_offset,
                "max_samples": self._samples.maxlen,
                "pair_max_dt_seconds": self.pair_max_dt_seconds,
                "pair_min_exposure_ratio": self.pair_min_exposure_ratio,
                "min_pairs": self.min_pairs,
                "max_pair_mad": self.max_pair_mad,
                "max_offset_deviation": self.max_offset_deviation,
                "max_age_seconds": self.max_age_seconds,
            },
            "samples_captured_at": [s[0] for s in self._samples],
            "samples_exposure_sec": [s[1] for s in self._samples],
            "samples_background_per_pixel": [s[2] for s in self._samples],
        }

    def reset(self) -> None:
        self._samples.clear()
        self._pedestal = None
        self._pair_mad = None
        self._n_pairs = 0
        self._accepted_at = None
