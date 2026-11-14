# Supported devices

The integration talks to devices over SSH using their command-line interface (CLI), so
support depends on the **CLI flavour**, not the exact model. There are two:

| CLI flavour | Prompt | Devices |
| --- | --- | --- |
| **HiveOS / IQ Engine** | `AH-7c0680#` | Aerohive and Extreme Networks access points, BR branch routers |
| **SR switch CLI** (FASTPATH-style) | `(AH-Switch) #` | Aerohive SR-series switches |

The flavour is detected automatically from the prompt when the device connects.

## Tested devices

| Device | Firmware | Status | Notes |
| --- | --- | --- | --- |
| Extreme Networks **AP460C** | IQ Engine 10.5r3 | ✅ Working | 2.4 GHz `wifi0`, 5 GHz `wifi1` (`wifi2` sensor radio unused). Parsers built from real output. |
| Aerohive **SR2208P** | Switch software 1.0.1.28 | ✅ Working | 8× PoE+ (1/0/1–1/0/8), 2 uplinks (1/0/9–1/0/10). All reads and PoE/port commands verified on the device. |
| Aerohive **BR100** | HiveOS (circa 2014) | 🟡 Partial | Works with the HiveOS parsers. `show interface` layout based on a synthetic sample; real output welcome. |

✅ Working: built and tested from real output of that device.
🟡 Partial: works, but some outputs or commands haven't been confirmed on that model.

## Expected to work (untested)

- **Other Aerohive / Extreme APs running HiveOS or IQ Engine** (AP1xx–AP6xx series, IQ
  Engine 6.x–10.x). They share the same CLI. Model-specific differences, usually in
  `show station` and `show interface wifiN`, are handled by tolerant parsers. Anything that
  isn't recognised is skipped instead of breaking the integration.
- **Other Aerohive SR-series switches running the same switch software** (prompt
  `(hostname) #`, ports named `1/0/N`). The PoE port count is read from the model
  description. Older SR switches that run HiveOS (prompt `AH-xxxxxx#`) use different
  commands, which are only guessed so far (see [CLI commands](cli-commands.md)).

If you run one of these, please [report it](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose)
(working or not) so this table can be updated.

## Not supported

- Extreme **EXOS / VOSS / Switch Engine** switches, and **WiNG** access points: different CLIs.
- Devices with SSH disabled, or reachable only through ExtremeCloud IQ / HiveManager.
- Cloud features (ExtremeCloud IQ / HiveManager configuration, licensing, reports).

## Feature support by device type

| Feature | APs (HiveOS / IQ Engine) | BR routers | SR switches |
| --- | --- | --- | --- |
| Firmware, model, serial, uptime, IP | ✅ | ✅ | ✅ |
| CPU / memory | ✅ (if `show cpu` / `show memory` exist) | ✅ (same) | – |
| Interface / port link, speed, throughput, counters | ✅ | ✅ | ✅ |
| Wi-Fi radios: channel, power, noise, utilisation | ✅ | ✅ | – |
| Wireless clients, per SSID / band, device trackers | ✅ | ✅ | – |
| Wi-Fi manager: edit / add / delete SSIDs, passwords, hidden | ✅ | ✅ | – |
| Wired clients per port (MAC table), wired trackers | – | 🟡 (if the firmware has a MAC table) | ✅ |
| LLDP neighbours (and LLDP discovery of unconfigured APs) | ✅ | ✅ | ✅ |
| PoE per port: power, status, on/off, power cycle | – | – | ✅ (PoE models) |
| Port on/off (uplinks protected) | – | 🟡 | ✅ |
| Reboot | ✅ | ✅ | ✅ |

See [CLI commands](cli-commands.md) for exactly which commands are used and which ones are
verified.

## Requesting support for a new device

Open a [device support request](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose).
The form asks for the output of a few `show` commands. The easiest way to get them is the
**Run show command** action in Home Assistant (Developer tools → Actions), which masks
passwords and keys automatically. Please remove serial numbers and anything else you
consider private before posting.
