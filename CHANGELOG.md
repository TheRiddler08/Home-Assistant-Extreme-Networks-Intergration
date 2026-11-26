# Changelog

All notable changes are listed here. Versions follow [semantic versioning](https://semver.org/).
GitHub releases start at 0.6.0. Earlier versions were installed from the branch.

## [0.23.0] - 2026-11-26

### Added
- **Access point only** for OpenWrt devices (Setup → Devices): use an OpenWrt router, such as
  the current one once a new router takes over, just for Wi-Fi. Its DHCP server, internet port,
  firewall and DHCPv6 are turned off, it uses the main router as gateway and DNS and keeps its
  address. Applied with the usual automatic rollback; the services are stopped only after the
  change is confirmed.

## [0.22.3] - 2026-11-24

### Fixed
- **OpenWrt: applies and the onboarding portal timed out with "Timed out waiting for prompt …
  (last output: '>')".** BusyBox's interactive shell (what an SSH session gets on OpenWrt) cuts
  every typed line off at 1024 characters; Home Assistant sent lines of up to 1500, so a long
  one lost its closing quote and the shell waited for more. Lines now stay under 900
  characters (network setup, portal files, fixed IPs/blocks/forwards, Wi-Fi tab changes), and
  a test replays every command in a real interactive BusyBox shell.

## [0.22.2] - 2026-11-24

### Changed
- "Timed out waiting for prompt" errors now include the last line the device printed (keys
  hidden), to see where a command got stuck.

## [0.22.1] - 2026-11-24

### Fixed
- **A router that's added but not working no longer just vanishes from the Setup tab.** It's
  listed under Devices with why (can't be reached, failed to set up, disabled), and the router
  choice says so.
- **OPNsense: an API key without rights to some pages no longer stops the router from
  loading.** Pages it may not read (port settings, gateways, temperature, ports…) are skipped
  and logged once; only the system information is required.
- **Aerohive access points: no bare `security-object <name>` any more.** The AP460C rejected
  it with "Invalid parameter(s)"; the object is now created by its first setting (password,
  then VLAN), like the user profile it accepted. Also for Wi-Fi networks added in the Wi-Fi tab.

## [0.22.0] - 2026-11-23

### Added
- **OPNsense ports:** turn assigned ports on and off and set fixed speeds (port dialog,
  `set_port`, `set_port_speed`), refusing the port Home Assistant uses; WAN needs `force`.
- **OPNsense configuration backups** with change-after-reboot alerts. Passwords, hashes, keys
  and other secrets are replaced by `***` before saving.
- **Onboarding portal on OPNsense:** served by Home Assistant on port 8099; the apply adds the
  router's forward from the onboarding network. Same pages, rules and approval.
- **IPv6 per network** (Setup → network dialog): addresses from the internet provider's
  delegated prefix, firewall rules for IPv6 included. OpenWrt (`ip6assign`, RA, DHCPv6) and
  OPNsense (track WAN).

## [0.21.0] - 2026-11-23

### Added
- **Fixed IPs with a local DNS name**, from a device's dialog in the Clients tab: the router
  always gives the device the same address and its name resolves locally. OpenWrt (`dhcp`
  host) and OPNsense (Kea reservation or Dnsmasq host, plus Unbound host override).
- **Block or pause a device** (1 hour, 8 hours, or any time through the new
  `aerohive_local.block_client` / `unblock_client` actions); pauses end on their own. The
  Clients tab marks blocked and paused devices. OpenWrt (firewall rules by MAC) and OPNsense
  (MAC alias + block rule).
- **Port forwards** card in the Setup tab: TCP/UDP/both, single ports or ranges, to another
  port, on/off. OpenWrt (firewall redirect) and OPNsense (destination NAT).
- These are saved on the router straight away and only touch what Home Assistant made.
  [Details](docs/network-setup.md#fixed-ips-blocked-devices-and-port-forwards)

### Changed
- OPNsense network setup: DHCP subnets are updated in place instead of recreated, so fixed
  IPs on them stay valid.

## [0.20.0] - 2026-11-23

### Added
- **OPNsense as the router in the network setup (preview).** Choose it under Setup →
  Devices → Router. Apply creates, through the API, for each network: a VLAN device on the
  trunk port (default: the LAN port), an `hn_` interface with the router address, a DHCP range
  on Kea or Dnsmasq (whichever is enabled) and firewall rules from the access grid. Only
  objects it made are changed (marked `[HA]` / `hn_`), never LAN, WAN or your own rules; a
  failed step removes what that apply added; the router is checked afterwards. The review
  lists every change. [Details](docs/opnsense.md#network-setup)

### Changed
- The Setup tab's router can be OpenWrt or OPNsense; "No router chosen" replaces "No OpenWrt
  router chosen".

## [0.19.0] - 2026-11-22

### Added
- **OPNsense routers (preview), through the REST API.** Add one with *OPNsense router (API
  key)*: host, API key and secret (System → Access → Users → API keys), with or without
  certificate checking. Read: hostname and version, CPU, uptime, memory, temperature,
  physical ports (link, speed, duplex, assignment, traffic, errors; WAN as uplink), assigned
  networks with VLAN tags, internet gateways (online, delay, loss; a gateway that's down is a
  problem), and devices behind the router with names and IPs from ARP and Kea / Dnsmasq / ISC
  DHCP leases. Reboot through the API. Re-authentication if the key stops working.
  [Details](docs/opnsense.md)
- Network panel: *Internet gateways* and *Networks* cards for routers that report them.

### Changed
- Save / Back up buttons and backup info are hidden for devices that don't have them
  (OPNsense saves every change itself; its configuration isn't copied, as it holds keys).

## [0.18.1] - 2026-11-20

### Fixed
- **Network setup on Aerohive access points: one rejected network no longer stops the rest.**
  Each Wi-Fi network is applied on its own; the result lists what failed and what was done
  (e.g. `homeOB: … rejected 'security-object homeOB' … (done: Guest)`). Seen on an AP460C,
  which rejected creating the homeOB security object; the cause is being looked into.
- **SR switch port errors say where and what:** `trunk ports: port 10 doesn't exist on this
  switch (it reports 1/0/1-1/0/9)`, naming the field (trunk ports or a network's access
  ports) and the ports the integration read from the switch.

## [0.18.0] - 2026-11-20

### Added
- **Client details.** The Clients tab shows each device's name, kind, maker and IP address;
  select one for everything known about it: name (and where it came from), maker from the
  MAC address (or that it's a private, randomised address), IP and hostname, where it's
  connected (access point, Wi-Fi network, band, channel, signal with SNR, speeds, connected
  time, security, VLAN; or switch port with its speed; or the router's LAN), the matching
  Home Assistant device (model, maker, area, link), and its per-device key.
  [Details](docs/dashboard.md#clients)
- **Names from more places:** your nickname, a Home Assistant device with the same MAC
  address, the device's per-device key or onboarding request, or its DHCP hostname.
- **Nicknames** for devices (administrators), shown everywhere in the panel, and
  **Disconnect** / **Open port** in the details.
- **IP addresses of wired devices** from ARP tables: ExtremeXOS `show iparp`, SR switches
  `show arp`, and an OpenWrt router's ARP table and DHCP leases. An OpenWrt router's wired
  LAN devices are now listed too.
- Devices in a port's dialog show their name, maker and IP, and open their details.

### Changed
- A device learned on several switches is listed once, at its edge port.
- New requirement: `aiooui` (offline maker lookup, also used by Home Assistant itself).

## [0.17.0] - 2026-11-19

### Added
- **Adjustable port speed.** Each port's dialog has a *Speed* setting: Auto (negotiate), or a
  fixed 10 / 100 Mbit/s with full or half duplex, 1 Gbit/s or 10 Gbit/s, as the port
  supports. It shows what the port is set to and the link speed; changing an uplink asks
  first. Also as the action `aerohive_local.set_port_speed`. ExtremeXOS uses its documented
  commands (1 Gbit/s copper keeps auto-negotiation, offering 1000 full only); SR switches and
  HiveOS Ethernet ports use **assumed** commands until confirmed on a device.
  [Details](docs/switches.md#port-speed)
- The configured speed is read back: ExtremeXOS from `show ports configuration`, SR switches
  and HiveOS from the running configuration.

## [0.16.0] - 2026-11-19

### Added
- **Key rotation.** *New key* on a device's key makes a new one that works alongside the old
  one; *Device updated* switches over, *Keep old* cancels. The old key can also stop by
  itself after a week, two weeks or a month. Rotate all keys of a network at once, or on a
  schedule (every 3 months, 6 months or a year) with a notification and the
  `aerohive_local_key_rotation` event. [Details](docs/network-setup.md#changing-keys-rotation)
- **Onboarding rules by kind of device**: Wi-Fi network, network (VLAN) and duration per kind;
  the approve dialog starts with them, and *Approve automatically* lets that kind in without
  asking. [Details](docs/onboarding.md#rules-by-kind-of-device)
- **Approve from a phone notification**: the actions `aerohive_local.approve_device` and
  `aerohive_local.decline_device` (administrators and automations), and richer
  `aerohive_local_onboarding_request` events (`kind_label`, `suggested_wifi`,
  `auto_approved`), with an example automation for Approve / Decline buttons.
- **Requests per hour** limit on the portal (default 10).
- **SR2208P VLANs in the network setup (assumed commands).** VLANs are read with
  `show vlan brief`, created and named in `vlan database`, tagged on trunk ports and set on
  access ports (PVID), and VLANs it created are removed when they leave the setup. VLAN 1 is
  never changed. The commands follow FASTPATH (which the switch's configuration shows) and
  aren't confirmed on a real SR2208P yet: the review says so.

### Changed
- Device-kind IDs no longer end with `_` when a long label is cut short (requests waiting
  with the old ID still show their kind's ID).

## [0.15.0] - 2026-11-19

### Added
- **Customise the onboarding portal** (*Customise portal* in the Setup tab), with a live
  preview of each page (ask, waiting, approved, declined):
  - every text: title, welcome text, button, waiting / approved (`{device}`, `{network}`) /
    declined messages, footer;
  - a logo (built into the page) and colours for button, background, text and fields;
  - your own list of device kinds, and whether devices choose the network;
  - **your own page** in HTML with `{{content}}` (required), `{{title}}`, `{{style}}`,
    `{{logo}}` and `{{footer}}`; *Start from the standard page* gives the default to edit.
  [Details](docs/onboarding.md#making-it-your-own)

### Changed
- The portal is generic: nothing in it is tied to a particular home layout, and the
  defaults are neutral. Title and welcome text moved from the card into *Customise portal*.
- Waiting page refreshes with an HTTP `Refresh` header as well, so it works with your own
  page.

### Fixed
- Panel dialogs: switching an option that reshapes a dialog (e.g. *Use my own page*) right
  after typing in a text box now redraws the dialog.

## [0.14.0] - 2026-11-19

### Added
- **Onboarding portal (phase 1).** New devices on the onboarding Wi-Fi (e.g. homeOB) open
  any web page and get a sign-in page served by the OpenWrt router: name, kind of device,
  which network. Home Assistant reads the requests over SSH (about every 15 s), shows them
  in the Setup tab with a notification and the `aerohive_local_onboarding_request` event.
  Approve (Wi-Fi network, VLAN, how long, optionally tied to the MAC) creates a per-device
  key, and the portal shows that device its password and QR code; decline tells it so.
  Onboarding devices never reach Home Assistant; only the asking browser (MAC + random
  token) sees the password; input is cleaned on the router; requests expire after an hour.
  Set up in the Setup tab's new *Onboarding portal* card. [Details](docs/onboarding.md)
- **QR codes** to join Wi-Fi: in a device key's dialog and with the new *QR* button on Wi-Fi
  networks that have a shared password.
- [Roadmap](docs/roadmap.md): what's planned and on hold (Dual WAN), and what the Aerohive
  per-device keys, WPA3 and client isolation need before they can be finished.

### Changed
- New requirement: `segno` (pure-Python QR codes).

## [0.13.0] - 2026-11-19

### Added
- **Network setup: design and change the whole network from Home Assistant** (new *Setup*
  tab in the Network panel, administrators only). [Details](docs/network-setup.md)
  - **Networks** (VLANs): router address, DHCP range and lease, internet access, access to
    the router, access ports on ExtremeXOS switches.
  - **Who can reach what**: a grid of networks against Internet, Router, main LAN and each
    other; it becomes the router's firewall (one zone per network, nothing else allowed).
  - **Wi-Fi networks**: network (VLAN), WPA2 / WPA2-WPA3 / WPA3 / open / **per-device keys**,
    bands, hidden, client isolation, max clients.
  - **Devices & keys**: a key per device (PPSK), optionally putting the device in another
    network, tied to a MAC address, or valid for 15 minutes to a week; show, revoke. Key
    changes take effect at once (Wi-Fi only is reloaded); timed keys are removed on their
    own, also after a restart.
  - **Review & apply**: the commands for every device (passwords hidden) and what each can't
    do, then applied switches first, router, access points, with a result per device.
  - **Start from the home layout**: the networks and Wi-Fi from the home network plan
    (VLANs 10–110), as a starting point.
  - Applied to **OpenWrt** (router: bridges per VLAN, addresses, DHCP, firewall zones, Wi-Fi,
    `wifi-station` keys; other OpenWrt APs: bridges and Wi-Fi), **Aerohive access points**
    (user profile per VLAN, security object, password, bands; not yet per-device keys, WPA3
    or isolation) and **ExtremeXOS switches** (VLANs, trunk and access ports).
  - **OpenWrt safety:** files are backed up and a timer on the router restores them after
    90 seconds unless Home Assistant reconnects and confirms; changes are only committed if
    every command was accepted; only `hn_*` sections are touched; unsaved LuCI changes stop
    the apply.
  - Passwords and keys are stored **encrypted** (separate key file), never shown in
    snapshots, states, events, the review or logs. Activity log of saves, applies and keys.

### Changed
- The panel's tabs scroll on narrow phones instead of being cut off.

## [0.12.1] - 2026-11-18

### Fixed
- **Wi-Fi editor: "Could not change Unknown … rejected 'no interface wifi0 ssid Unknown':
  ERROR: Invalid parameter(s)".** Some access points list entries that aren't networks in
  their configuration (one showed "Unknown"), and the integration offered them for editing.
  Now only networks the access point's configuration defines (`ssid <name>`) can be changed:
  - other entries are shown read-only in the Wi-Fi tab and on the access point's page, with
    an explanation, and get no switches, password or max-clients entities;
  - nothing is sent to the access point for them; actions explain why instead;
  - a network that one access point configures and another only lists is changed on the
    first only.

## [0.12.0] - 2026-11-18

### Added
- **Front panel shows more than data ports**, where the device reports it:
  - **SFP cages** drawn as cages (filled with a module, its type in the tooltip and port
    dialog; an empty slot without), and **combo** copper/SFP ports marked. ExtremeXOS reads
    them from `show ports configuration`; SR switches from the model string (*2 GE dual media
    ports*); OpenWrt from `sfp*` port names.
  - **Console** (ExtremeXOS; SR switches from `line console` in their configuration),
    **management port** with link and IP (ExtremeXOS), **USB** (a USB modem on BR100/APs,
    attached devices on OpenWrt) and **power supplies** (ExtremeXOS PSU 1 / PSU 2, including
    an empty backup-PSU slot or a failed one).
  - A port's type is shown in its dialog.
- Nothing is drawn that the device doesn't report: an unreported connector is unknown, not
  absent.

## [0.11.0] - 2026-11-18

### Added
- **OpenWrt routers and access points (preview).** Added by IP address with the SSH login
  and recognised from the shell prompt; nothing to install on the router. Read with `ubus`
  JSON and UCI: model, version, uptime, CPU, memory, temperature, Ethernet ports (link,
  speed, traffic; WAN marked as the uplink), radios (actual channel, width, power, noise),
  Wi-Fi networks, clients (names and IPs from the DHCP leases), wired clients, unsaved UCI
  changes, and configuration backups with change-after-reboot alerts.
- **Wi-Fi control on OpenWrt**, through the same Wi-Fi tab, card, entities and actions as on
  Aerohive APs: networks on/off (also for a while), bands, hidden name, password, max
  clients, add, delete, disconnect a client. Changes are UCI commands with
  `uci commit wireless && wifi reload`; network, firewall and DHCP settings are never touched.
  Wi-Fi keys never reach Home Assistant: they're filtered on the router, masked in backups,
  and copied on the router when a network is added to another band.
- Pretend OpenWrt router over SSH for development (`tests/frontend/mock_openwrt_router.py`).

### Changed
- The integration is now called **Aerohive, Extreme Networks & OpenWrt (Local SSH)**. The
  domain (`aerohive_local`) is unchanged, so existing devices, entities and automations stay
  as they are.
- Turning a Wi-Fi network back on uses the bands it's configured for (OpenWrt keeps disabled
  networks), instead of every radio.

### Fixed
- Wi-Fi actions in the Network panel didn't show their confirmation message or their
  "working…" state (the change itself was made).
- A device whose session was new chose how to poll before it knew the CLI (now it connects
  first). This mattered for OpenWrt, whose prompt identifies it.

## [0.10.2] - 2026-11-17

### Fixed
- **BR100 showed only some of its Ethernet ports** (for example 2 instead of 5): its
  `show interface` summary doesn't always list every port. BR100 and BR200 now always have
  eth0 (WAN) and eth1–eth4 (LAN); ports missing from the summary are read one by one
  (`show interface ethN`) for their link state, speed and traffic.

### Changed
- Branch routers are drawn as one row in the Network panel: WAN, then LAN 1–4.

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
