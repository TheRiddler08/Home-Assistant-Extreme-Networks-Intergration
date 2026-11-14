# Network overview ("Aerohive Network" device)

Created automatically once you add a device. It combines every AP and switch:

| Entity | Notes |
| --- | --- |
| Devices online / Access points online / Switches online, All devices online (binary) | attribute `devices`: name, model, IP, online, clients |
| Wireless clients (attribute `clients`: every client with its AP, SSID, band, signal) | plus Clients 2.4 GHz / 5 GHz |
| `<SSID> clients (all APs)` | per SSID, with a per-AP breakdown |
| SSIDs, SSIDs broadcasting | attribute `ssids` (see above) |
| Wireless RX / TX / total throughput, Wired RX / TX throughput | sums over all APs / switches |
| Wired clients, Switch ports up, PoE power used / budget | from all switches |
| SSID controls (all APs) | see the table above; the Wi-Fi manager is under this entry's **Configure** |

It sends no commands of its own. It aggregates what the devices already polled every 15 s,
and fans control actions out to each AP (a failure on one AP is reported, the others still
get the change). You can't add it twice; disable it if you don't want it.
