# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org/).
GitHub releases start at 0.6.0. Earlier versions were installed from the branch.

## [0.6.2] - 2026-11-14

### Changed
- Repository reorganised: short README, documentation in `docs/` (installation, supported
  devices, entities, Wi-Fi manager, overview, switches, actions, how it works, CLI commands,
  development).
- MIT license, changelog, contributing and security policy, GitHub issue forms (bug report,
  device support, feature request) and pull request template.
- Integration icon prepared for the Home Assistant brands repository.
- Manifest documentation and issue tracker links point to the new docs.

## [0.6.1] - 2026-11-10

### Fixed
- One garbled reply could disable `show ssid` / `show station` for the whole session. A
  command is now only skipped after being rejected on two polls in a row, and is retried
  every 30 minutes.
- An incomplete `show interface` reply could make an AP look like it had no Wi-Fi. It is
  now retried, and if it's still incomplete the last good data is kept.
- SSIDs on a radio with an `Auto` / DFS channel were shown as not broadcasting.
- Long SSID names cut short by the `show interface` table weren't matched. The running-config
  bindings are now used as well.
- An AP with every radio down wasn't counted as an AP.

## [0.6.0] - 2026-11-10

### Added
- **Wi-Fi manager** in the integration's Configure dialog (per AP, and for all APs on the
  *Aerohive Network* entry): list, edit (password, radios, hidden, max clients), add and
  delete SSIDs. All changes are saved together with a single save.
- Release workflow: GitHub releases are published automatically on version bumps.

### Removed
- The "New SSID name / password", "Create SSID", "SSID to delete" and "Delete selected
  SSID" entities (replaced by the Wi-Fi manager). They are removed automatically.

## [0.5.1]

### Fixed
- Entities of deleted SSIDs and untracked clients are removed instead of staying
  "unavailable".

## [0.5.0]

### Added
- *Aerohive Network* overview device combining every AP and switch, with network-wide SSID
  controls.
- "All ports" summary sensors on switches. Port on/off switches are enabled by default,
  with uplink protection and a `set_port` action.

### Changed
- Wi-Fi key texts and port switches created disabled by older versions are enabled once.

## [0.4.0]

### Added
- Aerohive SR2208P switch CLI support from real device output: ports, PoE, MAC table, LLDP,
  counters, uptime, idle-timeout recovery.
- LLDP discovery: unconfigured APs seen by a switch are offered for setup.

## [0.3.0]

### Added
- Network scan and DHCP discovery in the setup dialog, alongside manual setup.
- Switch support (ports, PoE, wired clients, LLDP), SSID hidden / all-radios switches,
  create/delete SSID actions, "Enable all entities" option.

## [0.2.0]

### Added
- Throughput, counters, CPU/memory, management IP, serial number, client lists for
  dashboards, per-client sensors, raw "every field" sensors.

## [0.1.0]

### Added
- Initial release: HiveOS / IQ Engine APs over SSH with legacy algorithms; sensors, SSID
  switches, max clients, Wi-Fi key, reboot, device trackers, debug `show` command action.

[0.6.2]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.6.2
[0.6.1]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.6.1
[0.6.0]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.6.0
