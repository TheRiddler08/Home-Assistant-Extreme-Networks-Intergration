# Supported devices

The integration talks to devices over SSH using their command-line interface (CLI), so
support depends on the **CLI flavour**, not the exact model. There are three:

| CLI flavour | Prompt | Devices |
| --- | --- | --- |
| **HiveOS / IQ Engine** | `AH-7c0680#` | Aerohive and Extreme Networks access points, BR branch routers |
| **SR switch CLI** (FASTPATH-style) | `(AH-Switch) #` | Aerohive SR-series switches |
| **ExtremeXOS** | `X450e-48p.3 #` | Extreme Networks Summit and other ExtremeXOS switches |

The flavour is detected automatically from the prompt when the device connects.

## Tested devices

| Device | Firmware | Status | Notes |
| --- | --- | --- | --- |
| Extreme Networks **AP460C** | IQ Engine 10.5r3 | ✅ Working | 2.4 GHz `wifi0`, 5 GHz `wifi1` (`wifi2` sensor radio unused). Parsers built from real output. |
| Aerohive **SR2208P** | Switch software 1.0.1.28 | ✅ Working | 8× PoE+ (1/0/1–1/0/8), 2 uplinks (1/0/9–1/0/10). All reads and PoE/port commands verified on the device. |
| Aerohive **BR100** | HiveOS (circa 2014) | 🟡 Partial | Works with the HiveOS parsers. All five ports (eth0 WAN, eth1–eth4 LAN) are shown; ports the `show interface` summary leaves out are read with `show interface ethN`. Real output welcome. |
| Extreme Networks **Summit X450e-48p** | ExtremeXOS 12.x / 15.x | 🧪 Preview | Built from the ExtremeXOS command reference and synthetic samples. Needs real output to be confirmed. See [below](#extremexos-switches). |

✅ Working: built and tested from real output of that device.
🟡 Partial: works, but some outputs or commands haven't been confirmed on that model.
🧪 Preview: implemented from documentation, not yet tested on the device.

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

## ExtremeXOS switches

The ExtremeXOS support targets the Summit X450e-48p, but uses commands common to ExtremeXOS
releases, so other ExtremeXOS switches (X440, X450, X460, X670…, including "Switch Engine",
the newer name for ExtremeXOS) are expected to work the same way.

Before adding one:

- **Enable SSH:** `enable ssh2`. On older releases (12.x) SSH is a separate module that may
  need to be installed first. If `enable ssh2` is rejected, check `show management`.
  Generate a host key with `configure ssh2 key` if asked.
- **Use an admin account.** A read-only `user` account gets a `>` prompt and can't read the
  configuration or change ports.

Please [report](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose)
how it works on yours.

## OPNsense

🧪 Preview: current OPNsense releases, read through the REST API with an API key (no SSH):
version, CPU, memory, temperature, ports, networks, internet gateways and the devices behind
the router; reboot. Ports are read-only, and configuration backups and the network setup's
router role aren't offered yet. See [OPNsense](opnsense.md).

## OpenWrt

🧪 Preview: OpenWrt 21.02 and later (routers and access points), read with `ubus` and UCI over
SSH, nothing to install on the router. Ports, radios, Wi-Fi networks and clients, health and
backups; Wi-Fi control (networks on/off, bands, password, hidden, max clients, add, delete,
disconnect clients). Ports are read-only and restore isn't offered. See [OpenWrt](openwrt.md).

## Not supported

- Extreme **VOSS / Fabric Engine** switches and **WiNG** access points: different CLIs.
- Devices with SSH disabled, or reachable only through ExtremeCloud IQ / HiveManager.
- Cloud features (ExtremeCloud IQ / HiveManager configuration, licensing, reports).

## Feature support by device type

| Feature | APs (HiveOS / IQ Engine) | BR routers | SR switches | ExtremeXOS switches |
| --- | --- | --- | --- | --- |
| Firmware, model, uptime, IP | ✅ | ✅ | ✅ | 🧪 |
| CPU / memory | ✅ (if `show cpu` / `show memory` exist) | ✅ (same) | – | 🧪 |
| Temperature, fans, power supply | – | – | – | 🧪 |
| Interface / port link, speed, throughput, counters | ✅ | ✅ | ✅ | 🧪 |
| Wi-Fi radios: channel, power, noise, utilisation | ✅ | ✅ | – | – |
| Wireless clients, per SSID / band, device trackers | ✅ | ✅ | – | – |
| Wi-Fi manager: edit / add / delete SSIDs, passwords, hidden | ✅ | ✅ | – | – |
| Wired clients per port (MAC table), wired trackers | – | 🟡 (if the firmware has a MAC table) | ✅ | 🧪 |
| LLDP neighbours (and LLDP discovery of unconfigured devices) | ✅ | ✅ | ✅ | 🧪 |
| PoE per port: power, status, on/off, power cycle | – | – | ✅ (PoE models) | 🧪 (PoE models) |
| Port on/off (uplinks protected) | – | 🟡 | ✅ | 🧪 |
| [Configuration backups, change-after-reboot alert, restore](backups.md) | ✅ | ✅ | ✅ | 🧪 |
| Boot configuration / image check, unsaved-changes alert | – | – | – | 🧪 |
| Reboot | ✅ | ✅ | ✅ | 🧪 |

See [CLI commands](cli-commands.md) for exactly which commands are used and which ones are
verified.

## Requesting support for a new device

Open a [device support request](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose).
The form asks for the output of a few `show` commands. The easiest way to get them is the
**Run show command** action in Home Assistant (Developer tools → Actions), which masks
passwords and keys automatically. Please remove serial numbers and anything else you
consider private before posting.
