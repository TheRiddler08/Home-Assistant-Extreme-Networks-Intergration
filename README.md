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
- **Clients to track**: pick connected clients, or type MAC addresses, to create
  `device_tracker` entities. Trackers are opt-in to avoid creating an entity for every phone that walks past.

### SSH algorithms

These devices only offer legacy algorithms, so the client explicitly allows
`diffie-hellman-group14-sha1`, `diffie-hellman-group1-sha1`, `hmac-sha1`,
`ssh-rsa`, and CBC ciphers (`aes128-cbc`, `aes256-cbc`, `3des-cbc`), after the modern ones.
This avoids the `no matching MAC found. Their offer: hmac-sha1` error. Host keys are
not verified (`known_hosts=None`), which is typical for LAN appliances.

## Entities

| Platform | Entity | Source |
| --- | --- | --- |
| sensor | Firmware (diagnostic, with firmware/build/bootloader attributes) | `show version` |
| sensor | Platform (diagnostic, disabled by default) | `show version` |
| sensor | Boot time (timestamp) | `show version` uptime |
| sensor | Connected clients (total) | `show station` |
| sensor | `<SSID> clients`, one per SSID | `show station` + `show interface` |
| sensor | `wifiN (band) clients`, `channel`, `channel width`, `SSIDs` | `show interface` / `show station` |
| sensor | `wifiN EIRP power`, `TX power`, `noise floor`, `TX/RX/interference/total utilization`, `max clients` (created only if the firmware reports them) | `show interface wifiN` |
| sensor | Region code, Country code (diagnostic) | `show boot-param` |
| binary_sensor | `<Interface> link` for mgt0, ethN, wifiN (connectivity; attributes: mode, MAC, VLAN, channel, radio profile…) | `show interface` |
| switch | `<SSID> on wifiN (band)`, one per SSID per active radio | `interface wifiN ssid <name>` / `no interface wifiN ssid <name>` |
| number | `<SSID> max clients` (1–255) | `ssid <name> max-client <n>` |
| text | `<SSID> Wi-Fi key` (write-only, **disabled by default**) | `security-object <obj> security protocol-suite wpa2-aes-psk ascii-key <key>` |
| button | Reboot | `reboot` (the confirmation prompt is answered automatically) |
| device_tracker | One per opted-in client MAC | `show station` |

A radio gets entities only if it has a channel or carries an SSID. On the AP460C, `wifi2`
(`Down`, no SSID) only gets a link binary sensor.

After every configuration change the integration runs `save config` and then refreshes.

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
