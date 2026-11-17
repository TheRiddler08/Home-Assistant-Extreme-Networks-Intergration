# Installation and setup

## Installation

Requires Home Assistant 2025.1 or newer, and SSH enabled on the devices (on ExtremeXOS
switches: `enable ssh2` and an admin account, see
[Supported devices](supported-devices.md#extremexos-switches)).

### HACS (recommended)

1. In HACS, open **⋮ → Custom repositories**. Skip this step once the integration is
   listed in the HACS default store.
2. Add `https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration`
   with category **Integration**.
3. Install **Aerohive / Extreme Networks (Local SSH)** and restart Home Assistant.

### Manual

Copy `custom_components/aerohive_local/` into `<config>/custom_components/` and
restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → Aerohive / Extreme Networks (Local SSH)**

You get a menu with two choices:

- **Scan my network**: the integration probes the local subnet(s) Home Assistant is on
  for SSH (port 22, at most 1024 addresses, 32 at a time) and lists what answers.
  Aerohive/Extreme devices are listed first, recognised by MAC prefix or by the default
  `AH-xxxxxx` hostname. Each entry shows what's known about it: IP address, name, model,
  MAC address and maker, and which switch port or access point it's connected to (from
  the devices you've already added). Devices already set up are marked *(already added)*.
  Pick one, enter the CLI login, and it's added. You can still type an IP into the device box.
- **Enter IP address manually**: the classic form.

**Automatic discovery:** if Home Assistant sees an Aerohive/Extreme device request a DHCP
lease, it shows up as **Discovered** under Settings → Devices & services, and you only need
to enter the login. The discovery card shows the device's MAC address, maker, model and
the switch port it's on, when known. Switches you've added also offer the Aerohive /
Extreme devices they see as LLDP neighbours. Matching is on the `AH-*` hostname and the MAC prefixes in
`manifest.json`. For devices that are already configured, DHCP discovery also updates the
stored IP address when it changes. Other devices on your network (phones, laptops, including tracked clients)
are never offered.

The username and port are pre-filled from devices you've already added.

| Field | Notes |
| --- | --- |
| Host | IP or hostname of the AP / router / switch |
| Username / Password | CLI credentials (default user on HiveOS is `admin`) |
| Port | Default `22` |
| SSH private key path | Optional. An absolute path readable by HA, e.g. `/config/.ssh/aerohive`. You can use it instead of, or as well as, a password |
| Scan interval | Default 60 s. Keep it ≥ 60 s for a BR100 (its CPU is weak) |

Add one config entry per device. The unique ID is the `mgt0` MAC address.

**Options** (the cog on the integration entry):

- **Scan interval**
- **Clients to track**: pick connected clients, or type MAC addresses, to create a
  `device_tracker` for each (plus signal/SNR/rate/SSID sensors on APs). On a switch, a
  tracked MAC is "home" while it's in the switch's MAC address table. This is opt-in to
  avoid creating entities for every phone that walks past. Every client always appears in
  the `clients` attribute of the clients sensors.
- **Read detailed statistics** (default on): throughput and counters for Ethernet ports and
  SSID sub-interfaces, plus CPU and memory.
- **Create sensors for every raw field** (default on): see [Entities → Every other field](entities.md#every-other-field-raw-sensors).
- **Enable all entities** (default off): switches on every entity this integration created
  disabled by default (diagnostics, counters, raw fields, port switches). Entities you
  disabled yourself stay disabled.

## After setup

A **Network** page appears in the sidebar with every device, switch front panels and
clients, and the dashboard card picker offers *Aerohive switch*, *Aerohive device* and
*Aerohive network* cards. See [Network panel and dashboard cards](dashboard.md).

## SSH algorithms

These devices only offer legacy algorithms, so the client explicitly allows
`diffie-hellman-group14-sha1`, `diffie-hellman-group1-sha1`, `hmac-sha1`,
`ssh-rsa`, and CBC ciphers (`aes128-cbc`, `aes256-cbc`, `3des-cbc`), after the modern ones.
This avoids the `no matching MAC found. Their offer: hmac-sha1` error. Host keys are
not verified (`known_hosts=None`), which is typical for LAN appliances.
