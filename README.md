# Aerohive / Extreme Networks (Local SSH) for Home Assistant

[![Validate](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/actions/workflows/validate.yml/badge.svg)](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/actions/workflows/validate.yml)
[![Release](https://img.shields.io/github/v/release/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration)](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Monitor and control **Aerohive / Extreme Networks access points, branch routers and SR
switches locally over SSH**. It doesn't use the cloud, HiveManager or ExtremeCloud IQ.

## Features

- **Auto-discovery:** scan the network, DHCP discovery, and APs found through a switch's
  LLDP neighbours, or add a device by IP.
- **Wi-Fi manager:** list, add, edit and delete SSIDs. Change passwords, choose radios,
  hide networks, set max clients, on one AP or on all of them at once.
- **Clients:** wireless and wired clients, per SSID, band and port, with signal and rates.
  Optional device trackers.
- **Radios:** channel, width, TX/EIRP power, noise floor, utilisation.
- **Switches:** port link, speed, throughput and counters; PoE power, status, on/off and
  power cycle; LLDP neighbours; port on/off with uplink protection.
- **System:** firmware, model, serial, uptime, CPU, memory, IP address, reboot.
- **Network overview:** one *Aerohive Network* device that combines every AP and switch.
- **Safe by design:** one throttled SSH session per device, changes verified against the
  device's reply, passwords never logged or stored in entity states.

## Supported devices

| Device | Status |
| --- | --- |
| Extreme Networks AP460C (IQ Engine 10.5) | ✅ Working |
| Aerohive SR2208P switch (software 1.0.1.28) | ✅ Working |
| Aerohive BR100 (HiveOS) | 🟡 Partial |
| Other HiveOS / IQ Engine APs, other SR switches on the same software | Expected to work, untested |

Full details: [Supported devices](docs/supported-devices.md).

## Installation

**HACS:** HACS → **⋮ → Custom repositories** → add
`https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration` as an
**Integration** → install **Aerohive / Extreme Networks (Local SSH)** → restart Home Assistant.

**Manual:** copy `custom_components/aerohive_local/` into your `config/custom_components/`
folder and restart Home Assistant.

Then go to **Settings → Devices & services → Add integration → Aerohive / Extreme
Networks (Local SSH)**, and choose **Scan my network** or **Enter IP address manually**.
[Installation and setup →](docs/installation.md)

## Documentation

| | |
| --- | --- |
| [Installation and setup](docs/installation.md) | Install, add devices, discovery, options |
| [Supported devices](docs/supported-devices.md) | Tested models, feature matrix, requesting support |
| [Entities](docs/entities.md) | Every sensor, switch and control, with a dashboard example |
| [Wi-Fi manager](docs/wifi.md) | Managing SSIDs and passwords |
| [Network overview](docs/overview.md) | The combined *Aerohive Network* device |
| [Switches](docs/switches.md) | SR-series switches: ports, PoE, LLDP |
| [Actions](docs/actions.md) | Actions for automations and debugging |
| [How it works](docs/how-it-works.md) | SSH session, safety, glitch protection |
| [CLI commands](docs/cli-commands.md) | Every command used, and which are verified |
| [Development](docs/development.md) | Tests, adding devices, releases |

## Getting help

- **Bug or problem:** [open a bug report](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose).
  Attach the diagnostics file (⋮ next to the device under Settings → Devices & services → Download diagnostics; it's redacted).
- **New device or firmware:** [request device support](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose).
- **Security issue:** see [SECURITY.md](SECURITY.md).

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Not affiliated with or endorsed by Extreme Networks. Aerohive, HiveOS and
IQ Engine are trademarks of their respective owners.
