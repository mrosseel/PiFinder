"""The integrator takes its solves from the sky simulator, with no camera,
no solver and no INDI server."""

import threading
import time
from queue import Queue

import pytest

from PiFinder import integrator
from PiFinder.state import SharedStateObj

pytestmark = pytest.mark.integration


def test_integrator_publishes_the_simulated_pointing(monkeypatch):
    # The integrator process sets up queue logging; this thread does not.
    monkeypatch.setattr(integrator.MultiprocLogging, "configurer", lambda q: None)
    shared_state = SharedStateObj()
    runner = threading.Thread(
        target=integrator.integrator,
        args=(shared_state, Queue(), Queue(), None),
        kwargs={"sky_sim": "tracking_m31"},
        daemon=True,
    )
    runner.start()
    deadline = time.time() + 15
    pointing = None
    while time.time() < deadline:
        solution = shared_state.solution()
        if solution is not None and solution.pointing.aligned.estimate is not None:
            pointing = solution.pointing.aligned.estimate
            break
        time.sleep(0.1)
    assert pointing is not None, "no solve from the sky simulator"
    assert pointing.RA == pytest.approx(10.6847, abs=0.05)
    assert pointing.Dec == pytest.approx(41.2687, abs=0.05)
