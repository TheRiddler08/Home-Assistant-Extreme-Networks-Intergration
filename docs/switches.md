# Switches

Two kinds of switches are supported: Aerohive **SR-series** switches (below), and
[**ExtremeXOS** switches](#extremexos-switches-summit-x450e) such as the Summit X450e.

## SR2208P and other SR-series

The SR2208P runs a FASTPATH-style switch CLI (prompt `(AH-Switch) #`, ports named `1/0/1`),
not HiveOS. The integration detects this from the prompt and uses the switch's own
commands. Verified against an SR2208P on software 1.0.1.28:

| Data | Command |
| --- | --- |
| Model, firmware, serial, MAC | `show version` |
| IP / netmask / gateway | `show network` |
| Port link, speed, duplex, admin state | `show port all` |
| PoE budget / consumed / remaining | `show poe` |
| PoE per port: power, max power, class, status, fault, voltage, current, temperature | `show poe port info 1/0/N` (PoE ports 1/0/1–1/0/8 only) |
| Clients per port | `show mac-addr-table` |
| Per-port octet/packet/error counters → throughput | `show interface ethernet 1/0/N` (ports that are up) |
| Uptime / boot time, port & PoE admin state | `show running-config` (header + `interface` blocks) |
| LLDP neighbours + their IP and capabilities | `show lldp remote-device all` + `… detail <port>` (every 15 min) |

On connect it answers a `User:` / `Password:` prompt if the switch asks again, runs `enable`
if the session starts at `>`, and runs `terminal length 0`. If the switch's idle timeout logs
the session out (`*** IDLE TIMEOUT ***`), the integration logs back in, re-enters `enable` and
re-runs the command.

| Entity | Notes |
| --- | --- |
| `Port 1/0/N link` binary sensor, link speed, duplex | per port (1/0/1–1/0/10; port-channels skipped) |
| `Port 1/0/N clients` (attribute `clients`: MAC + VLAN) | from the MAC table |
| `Wired clients` | MACs on access ports. **Uplink ports are excluded**: a port whose LLDP neighbour is a switch/router (e.g. 1/0/9 → your core switch), or one with ≥16 MACs |
| `Port 1/0/N LLDP neighbor` | system name (or chassis ID). Attributes: management IP, port ID, capabilities |
| PoE power used / budget / remaining | device level |
| IP address, firmware, serial number, reboot | `reload`, confirmations answered |
| `Port 1/0/N PoE power` (W), `PoE status` (Searching / Delivering…), `PoE fault status` | per PoE port |
| `Port 1/0/N PoE voltage`, `current`, `temperature`, `class`, `max power` | per PoE port, disabled by default |
| `Port 1/0/N PoE` switch | PoE on/off (`poe` / `no poe`), saved with `write memory` |
| `Port 1/0/N PoE power cycle` button | `poe reset`: reboots whatever the port powers (AP, camera, phone); config unchanged |
| `Port 1/0/N enabled` switch | port on/off (`shutdown` / `no shutdown`). **Uplink ports are protected**: turning off a port whose LLDP neighbour is a switch/router (or that has ≥16 devices behind it) is refused, since it could disconnect Home Assistant. Override with the `set_port` action and `force: true` |
| Ports up / Ports down / Ports disabled | "all ports" summary; `Ports up` has a `ports` attribute with one row per port (link, enabled, speed, uplink, clients, neighbour, PoE W/status, RX/TX kbit/s) |
| Wired total throughput, All ports RX/TX bytes, errors, discards | sums over all ports |
| RX / TX throughput per port | from `Total Packets Received/Transmitted (Octets)`. Counters (bytes, packets, errors, discards) are disabled by default; the other ~60 statistics lines are not turned into entities |

**Finds your APs for you:** when a configured switch or AP sees an Aerohive/Extreme device
as an LLDP neighbour, with a management IP, that isn't set up yet, it shows up as
**Discovered** in Settings → Devices & services, so you only need to enter the login.

Device trackers work for wired clients too. A tracked MAC is "home" while it's learned on
one of the switch's access ports.

## ExtremeXOS switches (Summit X450e)

🧪 Preview: built from the ExtremeXOS documentation, not yet tested on a device.

Detected from the prompt (`X450e-48p.3 #`). Polled with `show switch`, `show ports
no-refresh`, `show ports statistics no-refresh`, `show inline-power`, `show fdb` and more
(see [CLI commands](cli-commands.md#extremexos-summit-x450e-and-other-exos-switches)).

You get the same per-port entities as on SR switches: link, speed, duplex, throughput,
clients, LLDP neighbour, PoE power/status/on-off/power cycle, and port on/off with uplink
protection. On top of that:

- **Health:** temperature, fans, power supply, CPU, memory, system state, boot count.
- **Boot check:** booted vs selected image and configuration, with an alert (and a Repairs
  notice) if the switch came up with Factory Default or the wrong file.
- **Unsaved changes:** an alert while the prompt shows `*`, plus a **Save configuration**
  button.
- **Port names:** set a port's display string from the Network panel or with the
  `set_port_description` action.
- **VLANs:** VLAN list and the switch's management IP.
- **Front panel:** copper, SFP (with the module type) and combo ports, the management port's
  link, and both power supplies (`show ports configuration`, `show port mgmt information`,
  `show power`).
- **Safe reboot:** a reboot with unsaved changes is refused unless you choose to save or
  discard them ([`reboot` action](actions.md#aerohive_localreboot)).
- **Backups:** see [Power-loss protection and backups](backups.md).

The [Network panel](dashboard.md) shows the switch's front panel with every port; select a
port to manage it.

Requirements: SSH enabled (`enable ssh2`) and an admin account; see
[Supported devices](supported-devices.md#extremexos-switches).

## Port speed

Every switch port (and the Ethernet ports of HiveOS access points and branch routers) can be
set to **Auto** or a fixed speed in its port dialog or with
[`aerohive_local.set_port_speed`](actions.md#aerohive_localset_port_speed). Use a fixed speed
only when the device on the other end needs one, and set both ends the same: a mismatch
(e.g. one end fixed at 100 full, the other on Auto) gives a slow, error-prone link.

| Device | Speeds | Commands |
| --- | --- | --- |
| ExtremeXOS (X450e) | Auto, 10, 100, 1000 (copper); Auto, 1000, 10000 (SFP) | `configure ports <p> auto on`; `configure ports <p> auto off speed <10\|100> duplex <full\|half>`; 1 Gbit/s copper keeps auto-negotiation and only offers 1000 full: `configure ports <p> auto on speed 1000 duplex full` |
| SR switches 🧪 | Auto, 10, 100 (1 Gbit/s comes from Auto) | interface mode: `auto-negotiate`; `speed <10\|100> <full\|half>-duplex` (**assumed**, FASTPATH) |
| HiveOS Ethernet ports 🧪 | Auto, 10, 100, 1000 | `interface ethN speed <auto\|10\|100\|1000>`, `interface ethN duplex <auto\|full\|half>` (**assumed**) |
| OpenWrt | – | ports are read-only |

The setting is read back from the device (ExtremeXOS `show ports configuration`; SR and HiveOS
from the running configuration), and saved like any other change. Changing an uplink's speed
asks first, because a mismatch can cut Home Assistant off from the switch.
