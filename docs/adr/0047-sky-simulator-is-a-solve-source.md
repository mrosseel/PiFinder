# The sky simulator is a solve source in the integrator, with pluggable pointing sources

**Status: accepted.**

Mount control needs a closed loop: the mount moves, the plate solve sees the move, and PiFinder corrects the mount. Screens such as Locate and the chart need a position that changes in a known way. A desk has no sky, and the debug camera solves a fixed set of images, so it points at one place near Lyra.

The sky simulator (`sky_sim.py`, `--sky-sim`) makes `SuccessfulSolve` and `FailedSolve` messages from a model. The integrator takes them instead of the solver output, in the same place where it takes the messages of a telemetry replay. All consumers (UI, mount process, web, position server) read the result through `shared_state` as usual.

## Decision

- **The simulator replaces the solver output in the integrator.** It does not make camera frames. The solver keeps running on the debug camera, and the integrator drops its output.
- **A pointing source tells where the telescope points.** `mount` follows an INDI mount and removes the syncs it sees from the reported position. `fixed_radec` tracks one position. `fixed_altaz` stands still while the sky turns. `path` follows timed waypoints. Each source gives a JNow position and whether the telescope moves.
- **One solve model for all sources.** It adds the pointing error (a fixed offset), noise and random failures, and it fails a solve while the telescope moves (a slewing mount, or a path faster than `blur_deg_s`). The result goes to J2000, as a real solve.
- **Scenarios are JSON files.** `events` change settings at a time, for example clouds. A name loads `sky_sim/NAME.json` from the repository.
- **The alt/az conversion is a short formula, without refraction.** It agrees with skyfield to about 10 arcseconds, which is less than the solve noise.

## Considered options

- **A camera simulator that renders star frames (rejected).** It would also test the solver, the SQM and the auto exposure. But it needs a star catalog renderer, the solver is slow on a desktop, and the closed loop for mount control would be slower and less repeatable. The solver has its own tests with recorded frames.
- **Telemetry replay (kept for its own use).** A replay plays a recorded night. It cannot react to a mount, so it cannot test goto, sync and refine. The simulator does not replace it: a replay has real IMU data and real solve failures.
- **A simulator only for the INDI mount (replaced).** The first version had only the `mount` source. Without an INDI server it made no solves, so it was of no use for screen tests and demos.

## Consequences

- The simulator does not test the camera, the solver, the SQM or the auto exposure. The lens self-heal does not run with the simulator, because its solves have no optics.
- There is no simulated IMU. Between two solves the position does not change, and the IMU dead reckoning is not tested.
- The roll is a fixed setting. It does not follow the field rotation of an alt/az telescope.
- The alt/az sources need a location: from the settings, or the PiFinder location after a lock.
- `--sky-sim` is a development option. The NixOS image has the code, but the PiFinder service does not use the option.
