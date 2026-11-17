# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org/).
GitHub releases start at 0.6.0. Earlier versions were installed from the branch.

## [0.10.1] - 2026-11-17

### Fixed
- Phones, laptops and other clients tracked with a device tracker were offered as new
  Aerohive devices to set up. Home Assistant's DHCP discovery reports every device linked to
  the integration (that's how a configured AP's new IP address is picked up), and the
  integration didn't check what it was. Now only devices with an Aerohive / Extreme MAC
  prefix or an `AH-xxxxxx` hostname are offered; devices already set up still have their IP
  address updated.

## [0.10.0] - 2026-11-17

### Added
- **Manage Wi-Fi networks in the Network panel:** a *Wi-Fi* tab lists every network once
  across all access points. Turn networks on or off, edit the password, bands, hidden name
  and max clients (on all access points or chosen ones), add and delete networks. Only
  administrators can make changes; passwords go over the WebSocket connection, never the
  event bus.
- **Temporary changes:** turn a network off (or on) for 30 minutes to 24 hours. It goes back
  to exactly how it was afterwards, also after a Home Assistant restart.
- **Aerohive Wi-Fi card** (`custom:aerohive-wifi-card`) with the same switches and timers,
  for example for a guest network.
- `set_ssid` action: on/off (optionally with `duration`), bands, hidden, max clients, on every
  access point with the network or one.

### Changed
- Wi-Fi changes from Home Assistant show at once (like ports), and new or deleted networks
  appear without waiting for the next poll.

## [0.9.0] - 2026-11-17

### Added
- **Refresh now:** a *Refresh now* button on every device, a `refresh` action (one device or
  all), and refresh buttons in the Network panel and cards. Reads everything at once,
  including LLDP neighbours, health and the configuration.
- **More detail when a device is found:** the network scan list and discovery cards show
  the name, model, MAC address and maker, and the switch port or access point the device
  is connected to (from the devices already added). Devices already set up are marked.

### Changed
- **Port, PoE and Wi-Fi switches respond at once.** The new state shows as soon as the
  device accepts the command, instead of after the next poll, and flipping a switch back
  straight away now works (it used to be skipped while the old state was still shown).
  The configuration is saved in the background 2 seconds after the last change, so a burst
  of changes costs one save; a failed save is retried and shown as a problem.
- Shorter debounce (0.5 s) and less waiting between commands on switches, so polls and
  refreshes on SR and ExtremeXOS switches are about four times faster.
- A device already added isn't offered again through LLDP discovery under another address.

### Fixed
- A switch whose login prompt wasn't recognised was polled as an access point and showed up
  as an AP with nothing on it. The device type is now confirmed from `show version`, and a
  device whose replies aren't recognised is shown as *not recognised* with a pointer to
  request support, not as an access point.

## [0.8.0] - 2026-11-17

### Added
- **Network panel** in the sidebar: every device at a glance; a live switch front panel
  where you select a port to turn it or its PoE on and off, restart a PoE device, rename it,
  and see its LLDP neighbour, clients and traffic; Wi-Fi radios, networks and clients;
  VLANs and health; one-click Save / Restore last backup / Keep current / Reboot for the
  power-loss alerts; a searchable list of all clients. Works on phones and in dark mode.
- **Dashboard cards:** `custom:aerohive-switch-card` (front panel with port controls),
  `custom:aerohive-device-card` and `custom:aerohive-network-card`, with a card editor.
  Loaded automatically.
- Actions: `reboot` (with `save_changes`), `set_poe`, `set_port_description`
  (ExtremeXOS port names) and `accept_config`.
- ExtremeXOS: serial number (`show version`), VLANs and management IP (`show vlan`), fan
  speed, power usage, LLDP-MED model of neighbours.
- WebSocket API (`aerohive_local/snapshot`, `aerohive_local/subscribe`) for the panel and
  cards.

### Changed
- ExtremeXOS parsers checked against the example output in the ExtremeXOS command
  reference: stack prompts (`Slot-1 Stack.2 #`) and two-column `show switch` values on
  stacks, ELSM flags and VLAN counts in `show ports`, tagged ports in `show fdb`, fan trays,
  stack temperature and CPU rows. LLDP details are read with one command instead of one per
  port.
- Booting the other image (for example a staged upgrade) is reported, not raised as a boot
  problem; states such as MASTER and BACKUP no longer count as problems.

### Fixed
- ExtremeXOS reboot with unsaved changes: the "save and reboot? (y - save, n - reboot
  without save)" prompt is answered as asked instead of always `y`, and the Reboot button
  refuses until the changes are saved or the `reboot` action says what to do with them.

## [0.7.0] - 2026-11-16

### Added
- **ExtremeXOS switches (preview):** Summit X450e-48p and other ExtremeXOS switches are
  detected from the prompt (`X450e-48p.3 #`). Ports (link, speed, duplex, throughput,
  counters), PoE (budget, per-port power/status/class/voltage/current, on/off, power cycle),
  clients per port, LLDP neighbours (with discovery of unconfigured devices), temperature,
  fans, power supply, CPU, memory, boot count, booted image and configuration. Built from
  the ExtremeXOS documentation; real output is needed to confirm it.
- **Power-loss protection** for every device type:
  - automatic configuration backups (every 15 minutes and after changes, only when the
    configuration changed; last 20 kept in `/config/aerohive_local_backups/`)
  - "Configuration changed after reboot" alert and Repairs notice when a device comes back
    with a different configuration, keeping the known-good backup as the latest
  - "Boot configuration problem" alert (ExtremeXOS booted Factory Default, the wrong
    configuration or image, or isn't operational)
  - "Unsaved configuration changes" alert (ExtremeXOS `*` prompt)
  - Save configuration / Back up configuration / Accept current configuration buttons
  - `save_config`, `backup_config`, `list_backups` and `restore_config` actions
  - "Devices with configuration problems" sensor on the *Aerohive Network* device
- Extreme Networks MAC prefixes (`00:04:96`, `00:E0:2B`) for the network scan, DHCP and
  LLDP discovery.

### Changed
- Diagnostics redact every MAC address, serial number, IP address, hostname and LLDP
  neighbour identifier, including inside raw device fields.

### Fixed
- `Error: ...` replies (as ExtremeXOS reports errors) are recognised as failures.

## [0.6.3] - 2026-11-14

### Fixed
- Requirement relaxed to `asyncssh>=2.21.0`: Home Assistant now ships asyncssh 2.24.0, and the
  exact 2.21.0 pin conflicted with it (hassfest error, possible install failure on new
  Home Assistant versions).

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

[0.7.0]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.7.0
[0.6.3]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.6.3
[0.6.2]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.6.2
[0.6.1]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.6.1
[0.6.0]: https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases/tag/v0.6.0
