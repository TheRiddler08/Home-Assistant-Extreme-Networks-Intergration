# Aerohive / Extreme Networks – Local SSH integration for Home Assistant

Monitor and control Aerohive / Extreme Networks access points and branch routers
(HiveOS / IQ Engine) **locally over SSH**. It doesn't use the cloud, HiveManager or ExtremeCloud IQ.

Tested against output from:

| Device | Firmware | Radios |
| --- | --- | --- |
| Extreme Networks AP460C | IQ Engine 10.5r3 build-278650 | wifi0 2.4 GHz (`radio_axg0`), wifi1 5 GHz (`radio_axa0`), wifi2 unused |
| Aerohive BR100 | HiveOS (circa 2014) | wifi0 2.4 GHz (`radio_ng0`), eth0 WAN, eth1–eth4 |

> **Status:** the `show interface`, `show ssid` and `show version` parsers are built
> from real AP460C output. The `show station`, `show interface wifiN` and `show boot-param`
> parsers don't have real samples yet. They are written defensively, but please capture real
> output with the debug service (see below) and send it in so they can be finalised.

## Installation

### HACS (recommended)

1. In HACS, open **⋮ → Custom repositories**.
2. Add `https://github.com/theriddler08/home-assistant-extreme-networks-intergration`
   with category **Integration**.
3. Install **Aerohive / Extreme Networks (Local SSH)** and restart Home Assistant.

### Manual

Copy `custom_components/aerohive_local/` into `<config>/custom_components/` and
restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → Aerohive / Extreme Networks (Local SSH)**

| Field | Notes |
| --- | --- |
| Host | IP or hostname of the AP / router |
| Username / Password | CLI credentials (default user on HiveOS is `admin`) |
| Port | Default `22` |
| SSH private key path | Optional. An absolute path readable by HA, e.g. `/config/.ssh/aerohive`. You can use it instead of, or as well as, a password |
| Scan interval | Default 60 s. Keep it ≥ 60 s for a BR100 (its CPU is weak) |

Add one config entry per device. The unique ID is the `mgt0` MAC address.

**Options** (the cog on the integration entry):

- **Scan interval**
- **Clients to track**: pick connected clients, or type MAC addresses, to create a
  `device_tracker` plus signal/SNR/rate/SSID sensors for each. This is opt-in to avoid
  creating entities for every phone that walks past. Every client always appears in the
  `clients` attribute of the connected-clients sensors.
- **Read detailed statistics** (default on): throughput and counters for Ethernet ports and
  SSID sub-interfaces, plus CPU and memory.
- **Create sensors for every raw field** (default on): see "Every other field" below.

### SSH algorithms

These devices only offer legacy algorithms, so the client explicitly allows
`diffie-hellman-group14-sha1`, `diffie-hellman-group1-sha1`, `hmac-sha1`,
`ssh-rsa`, and CBC ciphers (`aes128-cbc`, `aes256-cbc`, `3des-cbc`), after the modern ones.
This avoids the `no matching MAC found. Their offer: hmac-sha1` error. Host keys are
not verified (`known_hosts=None`), which is typical for LAN appliances.

## Entities

Every value the integration can read becomes an entity. Core values are enabled. Verbose
diagnostic values are created **disabled**: enable them on the device page under
"+N entities not shown".

### Device

| Entity | Notes |
| --- | --- |
| Connected clients | Total. Attribute `clients`: a list of every client (MAC, IP, hostname, SSID, interface, band, channel, signal, SNR, TX/RX rate, PHY mode, auth, VLAN, connected time) for dashboards |
| SSIDs broadcasting | Count. Attributes `broadcasting`, `configured`, `not_broadcasting` |
| Wireless RX / TX / total throughput | Sum over the radios (RX = from clients, TX = to clients) |
| Wired RX / TX throughput | Sum over Ethernet ports |
| CPU usage (+ user/system, disabled) | `show cpu` |
| Memory usage, memory free (+ total, disabled) | `show memory` |
| IP address (netmask/gateway as attributes), default gateway (disabled) | `show interface mgt0` |
| Firmware, platform (disabled), boot time, uptime (disabled) | `show version` |
| Serial number, hardware revision (disabled) | `show hw-info` (also fills the device registry) |
| Region code, country code | `show boot-param` |

### Per radio (`wifi0 (2.4 GHz)`, `wifi1 (5 GHz)`, …)

Clients (with a `clients` list attribute), channel, channel width, SSIDs, EIRP power, TX power,
noise floor, TX / RX / interference / total utilisation, max clients, PHY mode, and
RX / TX throughput (from the interface section below).

### Per interface (eth*, wifi*, sub-interfaces like `wifi0.1 (AP460C)`, mgt0)

| Entity | Default |
| --- | --- |
| `<if> link` (binary sensor) | enabled |
| RX / TX throughput (kbit/s internally, shown in Mbit/s) | enabled |
| Link speed, duplex (Ethernet) | enabled |
| RX/TX bytes, packets, errors, dropped (total counters) | disabled |
| Mode, VLAN, hive, MAC address | disabled |

### Per SSID

| Entity | Default |
| --- | --- |
| `<SSID> broadcasting` (binary sensor: bound to at least one radio that is up; attributes: radios, sub-interfaces, clients, DTIM, RTS, frag, MAC filter) | enabled |
| `<SSID> clients` (attribute `clients` list) | enabled |
| `<SSID> RX / TX throughput` (sum of its sub-interfaces) | enabled |
| `<SSID> on wifiN (band)` switch, `<SSID> max clients` number | enabled |
| `<SSID> Wi-Fi key` text (write-only) | disabled |
| DTIM period, RTS threshold, fragmentation threshold, MAC filter | disabled |

### Per tracked client (opt-in in the options)

`device_tracker` (home / not_home), plus sensors for signal (dBm), SNR (dB), TX rate,
RX rate (Mbit/s), SSID, interface, IP address and connected time. They show unavailable
while the client is not connected.

### Every other field ("raw" sensors)

Any other `key=value` / `key: value` field reported by `show interface <name>`, `show cpu`,
`show memory`, `show hw-info`, `show boot-param` or `show version` gets its own sensor,
disabled by default (e.g. `wifi0 (2.4 GHz) mtu`, `Eth1 operational state`). Numbers keep
their unit (dBm, %, Mbit/s…). You can turn this off with **Create sensors for every raw
field** in the options.

### Other platforms

| Platform | Entity |
| --- | --- |
| button | Reboot |
| switch | SSID per radio (see above) |
| number | SSID max clients (1–255) |

A radio gets entities only if it has a channel or carries an SSID. On the AP460C, `wifi2`
(`Down`, no SSID) only gets a link binary sensor.

### Throughput notes

Throughput is computed from the byte counters in `show interface <name>`: the change since the
previous poll, divided by the elapsed time. It is `unknown` after the first poll, and an average
over the scan interval rather than an instantaneous value. A counter reset (reboot or wrap)
gives one `unknown` sample.

### Polling cost

Each poll, with **Read detailed statistics** on (the default): `show version`,
`show interface`, `show ssid`, `show station`, `show interface wifiN` for each active radio,
`show interface ethN` for each Ethernet port that is up, `show interface wifiN.M` for each
SSID sub-interface, `show cpu` and `show memory`. That's about 11 commands on an AP460C
(about 5 s at 0.3 s spacing). `show boot-param`, `show hw-info` and `show interface mgt0`
run every 15 minutes. With detailed statistics off, only the radio details are read. That
is the gentler choice for a BR100, or raise its scan interval.

### Dashboard example: client table

```yaml
type: markdown
title: Wi-Fi clients
content: |
  | Client | IP | SSID | Band | Signal | Rate |
  |---|---|---|---|---|---|
  {% for c in state_attr('sensor.ah_7c0680_connected_clients', 'clients') or [] -%}
  | {{ c.hostname or c.mac }} | {{ c.ip or '' }} | {{ c.ssid }} | {{ c.band or '' }} | {{ c.signal }} dBm | {{ c.tx_rate }} |
  {% endfor %}
```

Replace `ah_7c0680` with your device's entity prefix. The same `clients` attribute is on each
`<SSID> clients` and `wifiN clients` sensor, for per-SSID or per-band tables. The list is left
out of the recorder, so it doesn't grow your database.

## Services

### `aerohive_local.run_show_command` (debug, returns a response)

Runs a single read-only `show …` command and returns its raw output. Use it to capture
samples for parser development:

```yaml
action: aerohive_local.run_show_command
data:
  device_id: <pick your AP>
  command: show station
  redact: true   # masks "ascii-key …"/"password …" arguments
```

Run it from **Developer tools → Actions** and copy the response. Only commands that start with
`show` are accepted (letters, digits, spaces and `-_.:/"|`).

**Please send back the output of:** `show station` (with at least one client connected,
ideally on both radios), `show interface wifi0`, `show interface wifi1`, `show boot-param`,
and the same commands from the BR100.

### `aerohive_local.set_ssid_password`

```yaml
action: aerohive_local.set_ssid_password
data:
  device_id: <AP>
  ssid: AP460C
  password: "new-key-8-to-63-chars"
  security_object: AP460C   # optional
```

If you don't pass `security_object`, the integration looks it up from
`ssid <name> security-object <obj>` in `show running-config`, and falls back to the SSID name.
The key is never logged. It is masked in error messages and isn't stored in any entity state.

### `aerohive_local.disconnect_client`

```yaml
action: aerohive_local.disconnect_client
data:
  device_id: <AP>
  mac: "aa:bb:cc:dd:ee:ff"
```

## Behaviour and safety

- **One SSH session per device**: a persistent interactive shell is reused, and every
  command goes through a lock. There is at least 0.3 s between commands, and the session
  reconnects (with one retry) if it drops.
- **Paging**: on connect the integration sends `console page 0`. If the device rejects it,
  `--More--` prompts are answered with a space.
- **Error detection**: if the output of a config command contains `ERROR`, `Invalid`,
  `unknown keyword`, `Incomplete command` or `Ambiguous command`, the integration raises a
  `HomeAssistantError` carrying the device's message, and skips `save config`.
- **Debouncing**: changes to a switch or number within 1.5 s are coalesced. Only the final
  value is sent, and nothing is sent if the device already has it.
- **Secrets**: passwords and Wi-Fi keys are never logged. Diagnostics redact the host,
  username, password, key path, tracked MACs and client MAC/IP/hostnames.
- **Unsupported commands**: if an optional command (`show station`,
  `show interface wifiN`, `show boot-param`) is rejected, the integration stops sending it
  for the rest of that session.

## Assumptions to verify on the real devices

These commands and formats couldn't be checked against real hardware. Please test them and report back:

1. **`console page 0`** disables paging. It's possibly a persistent setting, so the next
   `save config` would store it in the device config, which also affects interactive
   console sessions. If you'd rather avoid that, tell me and I'll make it optional.
   Paging falls back to answering `--More--` either way.
2. **`show station` format.** The parser expects the classic HiveOS layout: an
   `Ifname=wifi0.1, Ifindex=13, SSID=<name>:` context line, then a table with
   `Mac Addr`, `IP Addr`, `Chan`, `Tx Rate`, `Rx Rate`, `Pow(SNR)`, `A-Mode`,
   `Cipher`, `A-Time`, `VLAN`, `Auth`, `UPID`, `Phymode` columns. If that doesn't match,
   it falls back to any line that starts with a MAC address.
3. **`show interface wifiN` keys.** The parser looks for `key=value;` pairs such as
   `Operational channel`, `Operational TX power`, `EIRP power`, `Noise floor`,
   `Tx/Rx/Interference/Total utilization` and `Max clients number`. Key names are matched
   loosely, but the real names may differ.
4. **`show boot-param`** contains `Region Code:` and `Country Code:` lines.
5. **Disconnecting a client** uses `clear auth station mac <aabb:ccdd:eeff>`. If the device
   rejects that, `clear auth station <aabb:ccdd:eeff>` is tried next. I'm least sure of
   this command, so please check `clear auth station ?` on the CLI.
6. **Reboot** asks a yes/no question (`(Y/N)`, `[y/n]`, `<yes/no>`…) and gets `y` + Enter
   as the answer. If it asks to save config first, that question is also answered `y`.
7. **`save config`** is the command that persists the running config. It's given 90 s to complete.
8. **`max-client` range** is assumed to be 1–255. The device reports an error if its range is smaller.
9. **PSK changes** use `wpa2-aes-psk` as you specified. An SSID on WPA3/SAE or WPA2/WPA3
   mixed mode would be switched to WPA2-PSK. Keys containing `"` are rejected because the
   CLI can't escape them.
10. **SSID names with spaces** are passed to the CLI in double quotes.
11. **BR100 `show interface`** is assumed to use the same table layout, without a channel
    width. A synthetic fixture covers this. Please send a real one.
12. **Prompt** is `<hostname>#`. A banner or MOTD before the first prompt is ignored.
13. **Traffic counters** come from `show interface <name>` as `Rx bytes=` / `Tx bytes=`
    (also matched: `*octets`, `Rx packets`, `Rx errors`, `Rx dropped`…). If your firmware
    has no byte counters, the throughput entities aren't created.
14. **Ethernet detail** reports `Speed=1000Mbps; Duplex=full-duplex;`.
15. **`show interface mgt0`** reports `IP addr=`, `Netmask=` and `Default Gateway=`.
16. **`show cpu`** reports lines like `CPU total utilization: 5.9%`, and **`show memory`**
    reports `Total Memory: … KB` / `Free Memory: … KB` (a Linux `free`-style table also works).
17. **`show hw-info`** reports `Serial number:` and `Hardware revision:`.
    Any of the commands in 13–17 that the firmware rejects are skipped quietly.

Please also send me `run_show_command` output for `show interface eth0`, `show interface wifi0.1`,
`show interface mgt0`, `show cpu`, `show memory` and `show hw-info`.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

The parser, command and SSH-client tests run without Home Assistant. The SSH-client tests
start a fake HiveOS SSH server on localhost that only offers the legacy algorithms, with
paging and a reboot confirmation. If `pytest-homeassistant-custom-component` is installed,
`tests/test_init.py` also covers the config flow, entity setup, controls, debouncing,
services and diagnostics.

`tests/fixtures/ap460c_*` files are real device output. `synthetic_*` files are
approximations until real samples are available.
