{ config, ... }:
{
  # ---------------------------------------------------------------------------
  # System clock from GPS and NTP (chrony)
  # ---------------------------------------------------------------------------
  # timesyncd only asks NTP servers, so in the field (no internet, AP mode) the
  # system clock of an RTC-less Pi 4 stays wrong. chrony also reads the GPS time
  # that gpsd writes to shared memory segment 0, and selects the better source
  # when NTP is reachable too. Enabling chrony disables timesyncd.
  #
  # Only the full system imports this module. The migration image has no gpsd
  # and keeps timesyncd (networking.nix), and device.nix waits for timesyncd.
  services.chrony = {
    enable = true;
    # The same pool servers as timesyncd (networking.nix).
    servers = config.services.timesyncd.servers;
    # The Pi 4 has no RTC to trim. This also permits rtcsync below.
    enableRTCTrimming = false;
    # At start, set the clock forward to the time of the drift file, which
    # chronyd writes when it stops. A cold boot then starts near the last
    # shutdown time, not at the build date.
    extraFlags = [ "-s" ];
    # The makestep line in extraConfig replaces the module's line, because the
    # module option does not accept the limit -1.
    makestep.enable = false;
    extraConfig = ''
      # The clock starts in the past at each cold boot and the first GPS fix
      # can come minutes later, so a step of more than 1 s is allowed at any
      # time, not only at the first updates.
      makestep 1 -1
      # GPS time from gpsd, without a PPS signal. NMEA/UBX time arrives about
      # 0.1 s to 0.5 s late and jitters, so the large delay keeps a reachable
      # NTP server preferred.
      refclock SHM 0 refid GPS precision 1e-1 offset 0.0 delay 0.5
      # Clears the kernel "unsynchronised" flag, so timedatectl shows
      # NTPSynchronized=yes, and keeps an RTC (Pi 5) in sync.
      rtcsync
    '';
  };
}
