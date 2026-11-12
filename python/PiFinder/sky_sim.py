"""The sky simulator: plate solves with no sky and no camera.

The integrator takes its solves from here instead of the solver while it
runs (``--sky-sim``). A pointing source tells where the telescope points.
The solve model then adds a pointing error, noise and failures, and makes
SuccessfulSolve/FailedSolve messages from the result.

Pointing sources ("source" in the settings):

- "mount": the position of an INDI mount (normally the INDI telescope
  simulator), so goto, refine, sync and drift compensation behave as on a
  real sky. The reported position includes the syncs. The simulator keeps
  the sum of the sync jumps, so physical = reported - syncs. A sync shows
  as a jump of the reported position while the mount is not slewing. The
  simulator watches every position update of the driver for this, so a
  short slew between two solves still counts as a slew. While the mount
  slews (state Busy) a solve fails: the frame has blur.
- "fixed_radec": a tracking telescope on one J2000 position.
- "fixed_altaz": a telescope that does not move, for example a Dobsonian.
  The sky drifts through the view.
- "path": timed waypoints, each a J2000 position ("ra", "dec") or an
  alt/az position ("alt", "az"). The telescope moves in a straight line
  between two waypoints and stays on the last one. A solve fails while the
  telescope moves faster than "blur_deg_s".

The alt/az sources need the observer location: "lat_deg" and "lon_deg" in
the settings, or else the location of PiFinder.

In all sources, the telescope points at the source position + pointing
error (a fixed offset that a scenario can change) + noise. A solve also
fails at the failure rate (clouds, too few stars).

The settings come from a JSON file, or a scenario name: NAME loads
sky_sim/NAME.json in the repository. "events" change settings at a time in
seconds after the start, for example clouds:

    {"rate_hz": 1.0, "offset_ra_deg": 1.5, "offset_dec_deg": -0.8,
     "noise_arcsec": 10, "failure_rate": 0.05, "seed": 1,
     "events": [{"t": 40, "failure_rate": 1.0}, {"t": 60, "failure_rate": 0.05}]}
"""

import dataclasses
import datetime
import json
import logging
import math
import random
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Union

from PiFinder import calc_utils, utils
from PiFinder.indi_client import TELESCOPE_INTERFACE, IndiClient, IndiProperty
from PiFinder.types.positioning import (
    FailedSolve,
    Pointing,
    SolveDiagnostics,
    SuccessfulSolve,
)

logger = logging.getLogger("SkySim")

POSITION = "EQUATORIAL_EOD_COORD"
# A jump of the reported position larger than this while the mount does not
# slew is a sync. Sidereal tracking keeps the EOD position still.
SYNC_JUMP_DEG = 0.02
SOURCES = ("mount", "fixed_radec", "fixed_altaz", "path")
SCENARIO_DIR = utils.pifinder_dir / "sky_sim"
# The time step to measure the speed of a path.
SPEED_DT_S = 0.2

# (lat, lon) in degrees, or None if not known.
LocationFn = Callable[[], Optional[tuple[float, float]]]
# (JNow RA, JNow Dec, moving) in degrees, or None if not known.
SourcePosition = Optional[tuple[float, float, bool]]


@dataclasses.dataclass
class SkySimSettings:
    source: str = "mount"
    rate_hz: float = 1.0
    offset_ra_deg: float = 1.5
    offset_dec_deg: float = -0.8
    noise_arcsec: float = 10.0
    failure_rate: float = 0.0
    roll_deg: float = 0.0
    seed: Optional[int] = None
    # "mount"
    indi_host: str = "localhost"
    indi_port: int = 7624
    # "fixed_radec" (J2000)
    ra_deg: Optional[float] = None
    dec_deg: Optional[float] = None
    # "fixed_altaz"
    alt_deg: Optional[float] = None
    az_deg: Optional[float] = None
    # "path"
    path: list = dataclasses.field(default_factory=list)
    blur_deg_s: float = 2.0
    # The observer location for the alt/az sources. None: PiFinder's.
    lat_deg: Optional[float] = None
    lon_deg: Optional[float] = None
    events: list = dataclasses.field(default_factory=list)

    @classmethod
    def load(cls, source: Union[str, Path, None]) -> "SkySimSettings":
        """From a JSON file or a scenario name, or the defaults for None or
        "default"."""
        if source is None or str(source) == "default":
            return cls()
        path = Path(source)
        if not path.is_file() and (SCENARIO_DIR / f"{source}.json").is_file():
            path = SCENARIO_DIR / f"{source}.json"
        values = json.loads(path.read_text())
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"unknown sky simulator settings: {sorted(unknown)}")
        settings = cls(**values)
        settings.check()
        return settings

    def check(self) -> None:
        """Raise ValueError if the source has not got the values it needs."""
        if self.source not in SOURCES:
            raise ValueError(f"sky simulator source must be one of {SOURCES}")
        if self.source == "fixed_radec" and None in (self.ra_deg, self.dec_deg):
            raise ValueError("source fixed_radec needs ra_deg and dec_deg")
        if self.source == "fixed_altaz" and None in (self.alt_deg, self.az_deg):
            raise ValueError("source fixed_altaz needs alt_deg and az_deg")
        if self.source == "path":
            if not self.path:
                raise ValueError("source path needs waypoints in path")
            for point in self.path:
                if "t" not in point or not (
                    {"ra", "dec"} <= set(point) or {"alt", "az"} <= set(point)
                ):
                    raise ValueError(
                        f"a path waypoint needs t and ra/dec or alt/az: {point}"
                    )


# Coordinates


def _gmst_deg(dt: datetime.datetime) -> float:
    """Greenwich mean sidereal time in degrees."""
    j2000 = datetime.datetime(2000, 1, 1, 12, tzinfo=datetime.timezone.utc)
    days = (dt - j2000).total_seconds() / 86400.0
    return (280.46061837 + 360.98564736629 * days) % 360.0


def altaz_to_radec(
    alt: float, az: float, lat: float, lon: float, dt: datetime.datetime
) -> tuple[float, float]:
    """The JNow RA/Dec in degrees of a geometric alt/az (azimuth from north
    through east). No refraction: a simulated sky needs none."""
    alt_r, az_r, lat_r = map(math.radians, (alt, az, lat))
    sin_dec = math.sin(alt_r) * math.sin(lat_r) + math.cos(alt_r) * math.cos(
        lat_r
    ) * math.cos(az_r)
    dec_r = math.asin(max(-1.0, min(1.0, sin_dec)))
    # sin(HA) cos(Dec) = -sin(Az) cos(Alt);
    # cos(HA) cos(Dec) = (sin(Alt) - sin(Lat) sin(Dec)) / cos(Lat).
    ha_r = math.atan2(
        -math.sin(az_r) * math.cos(alt_r) * math.cos(lat_r),
        math.sin(alt_r) - math.sin(lat_r) * sin_dec,
    )
    ra = (_gmst_deg(dt) + lon - math.degrees(ha_r)) % 360.0
    return ra, math.degrees(dec_r)


def _vector(ra: float, dec: float) -> tuple[float, float, float]:
    ra_r, dec_r = math.radians(ra), math.radians(dec)
    return (
        math.cos(dec_r) * math.cos(ra_r),
        math.cos(dec_r) * math.sin(ra_r),
        math.sin(dec_r),
    )


def interpolate(
    a: tuple[float, float], b: tuple[float, float], fraction: float
) -> tuple[float, float]:
    """The RA/Dec at a fraction of the way from a to b, along a great
    circle (normalized linear interpolation of the unit vectors)."""
    va, vb = _vector(*a), _vector(*b)
    v = [p + (q - p) * fraction for p, q in zip(va, vb)]
    norm = math.sqrt(sum(c * c for c in v)) or 1.0
    x, y, z = (c / norm for c in v)
    return math.degrees(math.atan2(y, x)) % 360.0, math.degrees(
        math.asin(max(-1.0, min(1.0, z)))
    )


def angular_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    va, vb = _vector(*a), _vector(*b)
    dot = sum(p * q for p, q in zip(va, vb))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


# Pointing sources


class MountSource:
    """The position of an INDI mount, without the syncs."""

    def __init__(self, settings: SkySimSettings, client: Optional[IndiClient]):
        self._client = client or IndiClient(settings.indi_host, settings.indi_port)
        self._client.add_listener(self._on_property)
        self._lock = threading.Lock()
        self._device: Optional[str] = None
        self._last_reported: Optional[tuple[float, float]] = None
        self._last_ok = False
        # The sum of the sync jumps in RA and Dec (degrees).
        self.sync_ra = 0.0
        self.sync_dec = 0.0

    def _mount_position(self) -> Optional[tuple[float, float, str]]:
        """The reported JNow RA/Dec in degrees and the property state."""
        if not self._client.is_connected() and not self._client.connect(timeout=1.0):
            return None
        if self._device is None:
            for device in self._client.devices():
                interface = self._client.device_interface(device)
                if interface is not None and interface & TELESCOPE_INTERFACE:
                    self._device = device
                    logger.info("Sky simulator follows %s", device)
                    break
        if self._device is None:
            return None
        prop = self._client.get_property(self._device, POSITION)
        if prop is None:
            return None
        ra = prop.elements.get("RA")
        dec = prop.elements.get("DEC")
        if not isinstance(ra, float) or not isinstance(dec, float):
            return None
        return ra * 15.0, dec, prop.state

    def _on_property(
        self,
        event: str,
        device: str,
        name: Optional[str],
        prop: Optional[IndiProperty],
    ) -> None:
        """Every position update of the mount (client reader thread)."""
        if event not in ("def", "set") or name != POSITION or prop is None:
            return
        if self._device is not None and device != self._device:
            return
        ra = prop.elements.get("RA")
        dec = prop.elements.get("DEC")
        if isinstance(ra, float) and isinstance(dec, float):
            with self._lock:
                self._track_syncs(ra * 15.0, dec, prop.state)

    def _track_syncs(self, ra: float, dec: float, state: str) -> None:
        """Add a jump of the reported position between two updates with state
        Ok to the sync record. A driver confirms a sync with state Ok; a slew
        reports Busy, and the first positions after the driver starts are
        Idle."""
        if self._last_reported is not None and state == "Ok" and self._last_ok:
            d_ra = (ra - self._last_reported[0] + 180.0) % 360.0 - 180.0
            d_dec = dec - self._last_reported[1]
            if abs(d_ra) > SYNC_JUMP_DEG or abs(d_dec) > SYNC_JUMP_DEG:
                self.sync_ra += d_ra
                self.sync_dec += d_dec
                logger.info("Sync seen: RA %+.4f°, Dec %+.4f°", d_ra, d_dec)
        self._last_reported = (ra, dec)
        self._last_ok = state == "Ok"

    def physical(self, reported_ra: float, reported_dec: float) -> tuple:
        """The physical position (JNow degrees) of a reported position."""
        with self._lock:
            sync_ra, sync_dec = self.sync_ra, self.sync_dec
        return reported_ra - sync_ra, reported_dec - sync_dec

    def position(self, elapsed: float, dt: datetime.datetime) -> SourcePosition:
        mount = self._mount_position()
        if mount is None:
            return None
        ra, dec, state = mount
        ra, dec = self.physical(ra, dec)
        return ra, dec, state == "Busy"


class FixedRaDecSource:
    """A tracking telescope on one J2000 position."""

    def __init__(self, settings: SkySimSettings):
        assert settings.ra_deg is not None and settings.dec_deg is not None
        self._radec = (settings.ra_deg, settings.dec_deg)

    def position(self, elapsed: float, dt: datetime.datetime) -> SourcePosition:
        ra, dec = calc_utils.j2000_to_jnow(*self._radec, dt)
        return ra, dec, False


class FixedAltAzSource:
    """A telescope that does not move: the sky drifts through the view."""

    def __init__(self, settings: SkySimSettings, location: LocationFn):
        assert settings.alt_deg is not None and settings.az_deg is not None
        self._altaz = (settings.alt_deg, settings.az_deg)
        self._location = location

    def position(self, elapsed: float, dt: datetime.datetime) -> SourcePosition:
        where = self._location()
        if where is None:
            return None
        ra, dec = altaz_to_radec(*self._altaz, *where, dt)
        return ra, dec, False


class PathSource:
    """Timed waypoints, J2000 RA/Dec or alt/az."""

    def __init__(self, settings: SkySimSettings, location: LocationFn):
        self._points = sorted(settings.path, key=lambda p: p["t"])
        self._blur_deg_s = settings.blur_deg_s
        self._location = location

    def _point(self, point: dict, dt: datetime.datetime):
        """A waypoint as JNow RA/Dec in degrees at time dt."""
        if "ra" in point:
            return calc_utils.j2000_to_jnow(point["ra"], point["dec"], dt)
        where = self._location()
        if where is None:
            return None
        return altaz_to_radec(point["alt"], point["az"], *where, dt)

    def _at(self, elapsed: float, dt: datetime.datetime):
        points = self._points
        if elapsed <= points[0]["t"]:
            return self._point(points[0], dt)
        for a, b in zip(points, points[1:]):
            if elapsed < b["t"]:
                start, end = self._point(a, dt), self._point(b, dt)
                if start is None or end is None:
                    return None
                fraction = (elapsed - a["t"]) / (b["t"] - a["t"])
                return interpolate(start, end, fraction)
        return self._point(points[-1], dt)

    def position(self, elapsed: float, dt: datetime.datetime) -> SourcePosition:
        now = self._at(elapsed, dt)
        before = self._at(elapsed - SPEED_DT_S, dt)
        if now is None or before is None:
            return None
        speed = angular_distance(now, before) / SPEED_DT_S
        return now[0], now[1], speed > self._blur_deg_s


# The simulator


class SkySimulator:
    """Makes SuccessfulSolve/FailedSolve messages from a pointing source."""

    def __init__(
        self,
        settings: SkySimSettings,
        now_utc: Callable[[], Optional[datetime.datetime]],
        client: Optional[IndiClient] = None,
        clock: Callable[[], float] = time.time,
        location: LocationFn = lambda: None,
    ):
        self.settings = settings
        self._now_utc = now_utc
        self._clock = clock
        self._random = random.Random(settings.seed)
        self._start: Optional[float] = None
        self._next_due = 0.0
        self._events = sorted(settings.events, key=lambda e: e["t"])
        self._location = location
        self.source: Union[MountSource, FixedRaDecSource, FixedAltAzSource, PathSource]
        if settings.source == "mount":
            self.source = MountSource(settings, client)
        elif settings.source == "fixed_radec":
            self.source = FixedRaDecSource(settings)
        elif settings.source == "fixed_altaz":
            self.source = FixedAltAzSource(settings, self.observer)
        else:
            self.source = PathSource(settings, self.observer)
        self._no_location_logged = False

    def observer(self) -> Optional[tuple[float, float]]:
        """The observer (lat, lon): from the settings, else PiFinder's."""
        if self.settings.lat_deg is not None and self.settings.lon_deg is not None:
            return self.settings.lat_deg, self.settings.lon_deg
        where = self._location()
        if where is None and not self._no_location_logged:
            logger.warning("Sky simulator: no location for an alt/az source")
            self._no_location_logged = True
        return where

    # Scenario

    def _apply_events(self, elapsed: float) -> None:
        while self._events and self._events[0]["t"] <= elapsed:
            event = self._events.pop(0)
            for key, value in event.items():
                if key != "t" and hasattr(self.settings, key):
                    setattr(self.settings, key, value)
            logger.info("Sky simulator event at %.0f s: %s", elapsed, event)

    # Solves

    def true_pointing(self, source_ra: float, source_dec: float) -> tuple:
        """Where the telescope points (JNow degrees), without noise."""
        ra = source_ra + self.settings.offset_ra_deg
        dec = source_dec + self.settings.offset_dec_deg
        return ra % 360.0, max(-90.0, min(90.0, dec))

    def next_message(self) -> Union[SuccessfulSolve, FailedSolve, None]:
        """The next solve when one is due, else None."""
        now = self._clock()
        if self._start is None:
            self._start = now
            self._next_due = now
        if now < self._next_due:
            return None
        self._next_due = now + 1.0 / max(self.settings.rate_hz, 0.01)
        elapsed = now - self._start
        self._apply_events(elapsed)

        dt = self._now_utc() or datetime.datetime.now(datetime.timezone.utc)
        position = self.source.position(elapsed, dt)
        if position is None:
            return None
        ra, dec, moving = position
        if moving or self._random.random() < self.settings.failure_rate:
            return FailedSolve(
                diagnostics=SolveDiagnostics(Matches=0),
                last_solve_attempt=now,
                last_solve_success=None,
            )

        true_ra, true_dec = self.true_pointing(ra, dec)
        noise = self.settings.noise_arcsec / 3600.0
        true_dec += self._random.gauss(0.0, noise)
        cos_dec = max(math.cos(math.radians(true_dec)), 1e-6)
        true_ra += self._random.gauss(0.0, noise) / cos_dec
        j2000_ra, j2000_dec = calc_utils.jnow_to_j2000(true_ra % 360.0, true_dec, dt)
        pointing = Pointing(RA=j2000_ra, Dec=j2000_dec, Roll=self.settings.roll_deg)
        return SuccessfulSolve(
            camera=pointing,
            aligned=pointing,
            imu_anchor=None,
            last_solve_attempt=now,
            last_solve_success=now,
            diagnostics=SolveDiagnostics(Matches=30, RMSE=noise * 3600.0),
        )
