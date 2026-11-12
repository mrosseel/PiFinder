"""The sky simulator (sky_sim.py): the pointing sources, with a fake INDI
client for the mount source."""

import datetime
import json

import pytest

from PiFinder import calc_utils
from PiFinder.indi_client import IndiProperty
from PiFinder import sky_sim
from PiFinder.sky_sim import SkySimSettings, SkySimulator
from PiFinder.types.positioning import FailedSolve, SuccessfulSolve

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 11, 11, 22, 0, tzinfo=datetime.timezone.utc)
DEVICE = "Telescope Simulator"


class FakeClient:
    def __init__(self):
        self.listeners = []
        self.position = IndiProperty(
            DEVICE, "EQUATORIAL_EOD_COORD", "number", elements={"RA": 2.0, "DEC": 20.0}
        )

    def add_listener(self, callback):
        self.listeners.append(callback)

    def is_connected(self):
        return True

    def connect(self, timeout=5.0):
        return True

    def devices(self):
        return [DEVICE]

    def device_interface(self, device):
        return 5

    def get_property(self, device, name):
        return self.position if name == "EQUATORIAL_EOD_COORD" else None

    def move(self, ra_hours, dec, state="Ok"):
        """The driver reports a new position."""
        self.position = IndiProperty(
            DEVICE,
            "EQUATORIAL_EOD_COORD",
            "number",
            state=state,
            elements={"RA": ra_hours, "DEC": dec},
        )
        for callback in self.listeners:
            callback("set", DEVICE, "EQUATORIAL_EOD_COORD", self.position)


def _sim(**settings):
    values = dict(offset_ra_deg=1.5, offset_dec_deg=-0.8, noise_arcsec=0.0, seed=1)
    values.update(settings)
    clock = [1000.0]
    client = FakeClient()
    sim = SkySimulator(
        SkySimSettings(**values), lambda: NOW, client=client, clock=lambda: clock[0]
    )
    return sim, client, clock


def _jnow(solve):
    return calc_utils.j2000_to_jnow(solve.aligned.RA, solve.aligned.Dec, NOW)


def test_solve_is_mount_position_plus_pointing_error():
    sim, _client, clock = _sim()
    solve = sim.next_message()
    assert isinstance(solve, SuccessfulSolve)
    assert _jnow(solve) == pytest.approx((30.0 + 1.5, 20.0 - 0.8), abs=1e-6)
    assert solve.last_solve_success == 1000.0
    # The next solve comes one period later.
    clock[0] += 0.5
    assert sim.next_message() is None
    clock[0] += 0.5
    assert isinstance(sim.next_message(), SuccessfulSolve)


def test_no_solve_while_the_mount_slews():
    sim, client, _clock = _sim()
    client.move(3.0, 25.0, state="Busy")
    assert isinstance(sim.next_message(), FailedSolve)


def test_a_sync_is_tracked_and_a_slew_is_not():
    sim, client, _clock = _sim()
    client.move(2.0, 20.0)
    # A sync: the reported position jumps while the mount stands still.
    client.move(2.1, 19.2)
    assert (sim.source.sync_ra, sim.source.sync_dec) == pytest.approx((1.5, -0.8))
    # The telescope still points where it pointed before the sync.
    assert sim.source.physical(31.5, 19.2) == pytest.approx((30.0, 20.0))
    assert _jnow(sim.next_message()) == pytest.approx((31.5, 19.2), abs=1e-6)
    # A slew: Busy, then the end position. No sync.
    client.move(2.5, 21.0, state="Busy")
    client.move(3.0, 22.0)
    client.move(3.0, 22.0)
    assert (sim.source.sync_ra, sim.source.sync_dec) == pytest.approx((1.5, -0.8))


def test_startup_positions_are_not_a_sync():
    """The driver first reports 0,0 and then the pole, with state Idle."""
    sim, client, _clock = _sim()
    client.move(0.0, 0.0, state="Idle")
    client.move(0.0, 90.0, state="Idle")
    client.move(0.0, 90.0)
    assert (sim.source.sync_ra, sim.source.sync_dec) == (0.0, 0.0)


def test_failures_and_scenario_events():
    sim, _client, clock = _sim(
        failure_rate=0.0,
        events=[{"t": 5, "failure_rate": 1.0}, {"t": 10, "offset_ra_deg": 0.0}],
    )
    assert isinstance(sim.next_message(), SuccessfulSolve)
    clock[0] += 5
    assert isinstance(sim.next_message(), FailedSolve)
    clock[0] += 5
    sim.settings.failure_rate = 0.0
    solve = sim.next_message()
    assert _jnow(solve)[0] == pytest.approx(30.0, abs=1e-6)


def test_noise_is_seeded_and_small():
    first, _c, _k = _sim(noise_arcsec=10.0, seed=7)
    second, _c, _k = _sim(noise_arcsec=10.0, seed=7)
    a, b = first.next_message(), second.next_message()
    assert (a.aligned.RA, a.aligned.Dec) == (b.aligned.RA, b.aligned.Dec)
    ra, dec = _jnow(a)
    assert abs(dec - 19.2) < 60 / 3600
    assert abs(ra - 31.5) < 60 / 3600


def test_settings_from_file(tmp_path):
    path = tmp_path / "sim.json"
    path.write_text(json.dumps({"rate_hz": 2.0, "offset_ra_deg": 0.5}))
    settings = SkySimSettings.load(path)
    assert (settings.rate_hz, settings.offset_ra_deg) == (2.0, 0.5)
    assert SkySimSettings.load("default") == SkySimSettings()
    path.write_text(json.dumps({"bogus": 1}))
    with pytest.raises(ValueError):
        SkySimSettings.load(path)


def test_scenario_by_name_and_bad_sources(tmp_path):
    """A name loads sky_sim/NAME.json; a source without its values fails."""
    assert SkySimSettings.load("clouds").events
    for name in ("dobsonian", "tracking_m31", "pushto_m13"):
        assert SkySimSettings.load(name).source != "mount"
    path = tmp_path / "sim.json"
    for values in (
        {"source": "bogus"},
        {"source": "fixed_radec", "ra_deg": 1.0},
        {"source": "fixed_altaz"},
        {"source": "path", "path": [{"t": 0, "ra": 1.0}]},
    ):
        path.write_text(json.dumps(values))
        with pytest.raises(ValueError):
            SkySimSettings.load(path)


def _scripted(clock_start=1000.0, location=None, **settings):
    values = dict(offset_ra_deg=0.0, offset_dec_deg=0.0, noise_arcsec=0.0, seed=1)
    values.update(settings)
    clock = [clock_start]
    sim = SkySimulator(
        SkySimSettings(**values),
        lambda: NOW,
        clock=lambda: clock[0],
        location=location or (lambda: None),
    )
    return sim, clock


def _j2000(solve):
    return solve.aligned.RA, solve.aligned.Dec


def test_fixed_radec_needs_no_mount():
    sim, _clock = _scripted(source="fixed_radec", ra_deg=10.68, dec_deg=41.27)
    assert _j2000(sim.next_message()) == pytest.approx((10.68, 41.27), abs=1e-6)


def test_altaz_to_radec():
    """The zenith is at Dec = latitude and RA = local sidereal time; due
    south the hour angle is 0."""
    lat, lon = 51.0, 4.0
    lst = (sky_sim._gmst_deg(NOW) + lon) % 360.0
    ra, dec = sky_sim.altaz_to_radec(90.0, 0.0, lat, lon, NOW)
    assert dec == pytest.approx(lat)
    ra, dec = sky_sim.altaz_to_radec(45.0, 180.0, lat, lon, NOW)
    assert (ra, dec) == pytest.approx((lst, lat - 45.0))
    # East of the meridian the hour angle is negative: RA > LST.
    ra, _dec = sky_sim.altaz_to_radec(30.0, 100.0, lat, lon, NOW)
    assert (ra - lst) % 360.0 < 180.0


def test_fixed_altaz_drifts_and_needs_a_location():
    sim, _clock = _scripted(source="fixed_altaz", alt_deg=45.0, az_deg=180.0)
    assert sim.next_message() is None
    sim, _clock = _scripted(
        source="fixed_altaz",
        alt_deg=45.0,
        az_deg=180.0,
        location=lambda: (51.0, 4.0),
    )
    first = _j2000(sim.next_message())
    assert first[1] == pytest.approx(6.0, abs=0.3)
    # One hour later the sky has turned 15 degrees through the view.
    later = NOW + datetime.timedelta(hours=1)
    ra_now, _dec = sky_sim.altaz_to_radec(45.0, 180.0, 51.0, 4.0, NOW)
    ra_later, _dec = sky_sim.altaz_to_radec(45.0, 180.0, 51.0, 4.0, later)
    assert (ra_later - ra_now) % 360.0 == pytest.approx(15.04, abs=0.01)
    # The settings location comes before PiFinder's.
    sim, _clock = _scripted(
        source="fixed_altaz",
        alt_deg=45.0,
        az_deg=180.0,
        lat_deg=-30.0,
        lon_deg=4.0,
        location=lambda: (51.0, 4.0),
    )
    assert _j2000(sim.next_message())[1] == pytest.approx(-75.0, abs=0.3)


def test_path_moves_blurs_and_stops():
    path = [
        {"t": 0, "ra": 100.0, "dec": 0.0},
        {"t": 10, "ra": 100.0, "dec": 0.0},
        {"t": 20, "ra": 110.0, "dec": 0.0},
        {"t": 22, "ra": 120.0, "dec": 0.0},
    ]
    sim, clock = _scripted(source="path", path=path, blur_deg_s=2.0)
    assert _j2000(sim.next_message()) == pytest.approx((100.0, 0.0), abs=1e-6)
    # Half way through the slow leg (1 deg/s): a solve.
    clock[0] += 15
    assert _j2000(sim.next_message()) == pytest.approx((105.0, 0.0), abs=1e-6)
    # The fast leg (5 deg/s): blur.
    clock[0] += 6
    assert isinstance(sim.next_message(), FailedSolve)
    # After the last waypoint the telescope stays there.
    clock[0] += 10
    assert _j2000(sim.next_message()) == pytest.approx((120.0, 0.0), abs=1e-6)
