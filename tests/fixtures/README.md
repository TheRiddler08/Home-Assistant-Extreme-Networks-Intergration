# Fixtures

- `ap460c_*.txt`: real output from an Extreme Networks AP460C, IQ Engine 10.5r3
  build-278650. `ap460c_show_version.txt` has a completed uptime line because the
  original sample was truncated (`test_show_version_truncated_uptime` covers the
  truncated form).
- `synthetic_*.txt`: hand-written approximations of HiveOS output. Replace them with
  real captures from `aerohive_local.run_show_command` when they're available.
