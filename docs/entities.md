# Entities

Every value the integration can read becomes an entity. Core values are enabled. Verbose
diagnostic values are created **disabled**: enable them on the device page under
"+N entities not shown".

## Device

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

## Per radio (`wifi0 (2.4 GHz)`, `wifi1 (5 GHz)`, …)

Clients (with a `clients` list attribute), channel, channel width, SSIDs, EIRP power, TX power,
noise floor, TX / RX / interference / total utilisation, max clients, PHY mode, and
RX / TX throughput (from the interface section below).

## Per interface (eth*, wifi*, sub-interfaces like `wifi0.1 (AP460C)`, mgt0)

| Entity | Default |
| --- | --- |
| `<if> link` (binary sensor) | enabled |
| RX / TX throughput (kbit/s internally, shown in Mbit/s) | enabled |
| Link speed, duplex (Ethernet) | enabled |
| RX/TX bytes, packets, errors, dropped (total counters) | disabled |
| Mode, VLAN, hive, MAC address | disabled |

## Per SSID

| Entity | Default |
| --- | --- |
| `<SSID> broadcasting` (binary sensor: bound to at least one radio that is up; attributes: radios, sub-interfaces, clients, DTIM, RTS, frag, MAC filter) | enabled |
| `<SSID> clients` (attribute `clients` list) | enabled |
| `<SSID> RX / TX throughput` (sum of its sub-interfaces) | enabled |
| `<SSID> on wifiN (band)` switch, `<SSID> max clients` number | enabled |
| `<SSID> Wi-Fi key` text (write-only: type a new key, it is never shown) | enabled |
| `<SSID> on all radios` switch, `<SSID> hidden` switch, `<SSID> security` sensor | enabled |
| DTIM period, RTS threshold, fragmentation threshold, MAC filter | disabled |

## Per tracked client (opt-in in the options)

`device_tracker` (home / not_home), plus sensors for signal (dBm), SNR (dB), TX rate,
RX rate (Mbit/s), SSID, interface, IP address and connected time. They show unavailable
while the client is not connected.

## Every other field ("raw" sensors)

Any other `key=value` / `key: value` field reported by `show interface <name>`, `show cpu`,
`show memory`, `show hw-info`, `show boot-param` or `show version` gets its own sensor,
disabled by default (e.g. `wifi0 (2.4 GHz) mtu`, `Eth1 operational state`). Numbers keep
their unit (dBm, %, Mbit/s…). You can turn this off with **Create sensors for every raw
field** in the options.

## Other platforms

| Platform | Entity |
| --- | --- |
| button | Reboot |
| switch | SSID per radio (see above) |
| number | SSID max clients (1–255) |

A radio gets entities only if it has a channel or carries an SSID. On the AP460C, `wifi2`
(`Down`, no SSID) only gets a link binary sensor.

## Throughput notes

Throughput is computed from the byte counters in `show interface <name>`: the change since the
previous poll, divided by the elapsed time. It is `unknown` after the first poll, and an average
over the scan interval rather than an instantaneous value. A counter reset (reboot or wrap)
gives one `unknown` sample.

## Polling cost

Each poll, with **Read detailed statistics** on (the default): `show version`,
`show interface`, `show ssid`, `show station`, `show interface wifiN` for each active radio,
`show interface ethN` for each Ethernet port that is up, `show interface wifiN.M` for each
SSID sub-interface, `show cpu` and `show memory`. That's about 11 commands on an AP460C
(about 5 s at 0.3 s spacing). `show boot-param`, `show hw-info` and `show interface mgt0`
run every 15 minutes. With detailed statistics off, only the radio details are read. That
is the gentler choice for a BR100, or raise its scan interval.

## Dashboard example: client table

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
