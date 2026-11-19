# VM test of the GPS time path: fake NMEA -> gpsd (-n, shared memory segment
# 0) -> chrony (nixos/time.nix) -> system clock. The fake GPS is a TCP feed,
# because gpsd never gives time from a pty to NTP.
#
#   nix build -L .#checks.x86_64-linux.chrony-gps
#
# The fake GPS reports a time one hour ahead of the VM clock, and the VM has no
# network, so the test passes only if chrony selects the GPS source and steps
# the clock to the GPS time. gpsd starts after chrony, as on the device, where
# gpsd starts when udev finds the GPS.
{ pkgs, ... }:
let
  feeder = pkgs.writers.writePython3 "fake-nmea" { } ''
    import functools
    import operator
    import sys
    import time

    OFFSET = 3600


    def sentence(body):
        cs = functools.reduce(operator.xor, body.encode(), 0)
        return "$%s*%02X\r\n" % (body, cs)


    # GPS time counts from a fixed start, so a step of the VM clock does
    # not move it.
    base = time.time() + OFFSET - time.monotonic()
    out = sys.stdout
    while True:
        now = int(base + time.monotonic())
        t = time.gmtime(now)
        hms = time.strftime("%H%M%S", t)
        dmy = time.strftime("%d%m%y", t)
        out.write(sentence(
            "GPGGA,%s.00,5100.000,N,00400.000,E,1,08,1.0,10.0,M,47.0,M,,"
            % hms))
        out.write(sentence(
            "GPGSA,A,3,01,02,03,04,05,06,07,08,,,,,1.8,1.0,1.5"))
        out.write(sentence(
            "GPRMC,%s.00,A,5100.000,N,00400.000,E,0.0,0.0,%s,,,A"
            % (hms, dmy)))
        out.flush()
        with open("/run/fake-gps/expected", "w") as f:
            f.write("%d\n" % now)
        time.sleep(1 - (base + time.monotonic()) % 1)
  '';
in
{
  name = "chrony-gps";

  nodes.machine = { lib, ... }: {
    imports = [ ../../time.nix ];

    # No network in the VM: the GPS must be the only source.
    services.chrony.servers = lib.mkForce [ ];

    users.users.gpsd = {
      isSystemUser = true;
      group = "gpsd";
    };
    users.groups.gpsd = { };

    # A TCP server stands in for the GPS UART.
    systemd.services.fake-gps = {
      serviceConfig = {
        RuntimeDirectory = "fake-gps";
        ExecStart = "${pkgs.socat}/bin/socat TCP-LISTEN:5000,bind=127.0.0.1,reuseaddr,fork EXEC:${feeder}";
      };
    };
    # The same -n option as the device (services.nix).
    systemd.services.gpsd-test = {
      requires = [ "fake-gps.service" ];
      after = [ "fake-gps.service" ];
      serviceConfig.ExecStart = "${pkgs.gpsd}/bin/gpsd -N -n tcp://127.0.0.1:5000";
    };
  };

  testScript = ''
    machine.wait_for_unit("chronyd.service")
    print(machine.succeed(
        "grep -v '^\\s*$' $(systemctl show -p ExecStart chronyd"
        " | grep -o '/nix/store/[^ ;]*chrony.conf')"
    ))

    machine.succeed("systemctl start fake-gps")
    machine.wait_for_open_port(5000)
    machine.succeed("systemctl start gpsd-test")

    # The GPS is the selected source ("#*").
    machine.wait_until_succeeds(
        "chronyc -n sources | tee /dev/stderr | grep -E '^#\\* GPS'", timeout=300
    )

    # The clock is stepped to the GPS time (one hour ahead of the boot clock).
    expected = int(machine.succeed("cat /run/fake-gps/expected"))
    now = int(machine.succeed("date +%s"))
    print(f"expected={expected} now={now}")
    assert abs(now - expected) <= 3, f"clock not stepped: {now} vs {expected}"

    # rtcsync clears the kernel flag, so timedatectl reports a synchronised clock.
    machine.wait_until_succeeds(
        "timedatectl show -p NTPSynchronized --value | grep -x yes", timeout=120
    )
    print(machine.succeed("chronyc tracking"))

    # With no GPS and no network, chronyd -s sets the clock forward to the
    # time of the drift file (the last stop of chronyd).
    machine.succeed("systemctl stop gpsd-test fake-gps chronyd")
    target = int(machine.succeed("date +%s")) + 86400
    machine.succeed(f"touch -d @{target} /var/lib/chrony/chrony.drift")
    machine.succeed("systemctl start chronyd")
    now = int(machine.succeed("date +%s"))
    print(f"drift file={target} now={now}")
    assert target <= now <= target + 30, f"clock not restored: {now} vs {target}"
  '';
}
