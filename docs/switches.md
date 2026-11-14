# Switches (SR2208P and other SR-series)

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
