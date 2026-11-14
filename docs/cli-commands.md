# CLI commands

Every command the integration sends, what it's used for, and whether it has been verified
on real hardware. Commands marked 🟡 follow the documented CLI syntax but haven't been
confirmed on a device yet. If one fails, the integration shows the device's error message
and changes nothing.

## HiveOS / IQ Engine (APs, BR routers)

### Read (every poll unless noted)

| Command | Used for | Status |
| --- | --- | --- |
| `show version` | Firmware, model, uptime | ✅ AP460C |
| `show interface` | Interfaces, radios, channels, SSID sub-interfaces | ✅ AP460C |
| `show ssid` | SSIDs, max clients, DTIM, RTS | ✅ AP460C |
| `show station` | Wireless clients | 🟡 parser built to the documented layout; falls back to any line starting with a MAC |
| `show interface <name>` | Radio power / noise / utilisation, traffic counters, Ethernet speed | 🟡 key names matched loosely |
| `show cpu`, `show memory` | CPU / memory sensors | 🟡 |
| `show running-config` | SSID bindings, security objects, hidden SSIDs (after changes, and every 15 min) | 🟡 |
| `show boot-param`, `show hw-info`, `show interface mgt0` | Region / country, serial, management IP (every 15 min) | 🟡 |
| `show lldp neighbor` | LLDP neighbours (every 15 min) | 🟡 |
| `console page 0` | Disable paging at login. Possibly persistent: stored on the next `save config`. `--More--` prompts are answered either way | 🟡 |

### Change

| Command | Used for | Status |
| --- | --- | --- |
| `interface wifiN ssid <name>` / `no …` | Broadcast an SSID on a radio | 🟡 |
| `ssid <name> max-client <n>` | Max clients (1–255) | 🟡 |
| `ssid <name> hide-ssid` / `no …` | Hide the SSID name | 🟡 |
| `security-object <obj> security protocol-suite wpa2-aes-psk ascii-key <key>` | Wi-Fi password. Switches the SSID to WPA2-PSK. Keys containing `"` are refused | 🟡 |
| `security-object <n>`, `ssid <n>`, `ssid <n> security-object <n>` | Create an SSID | 🟡 |
| `no ssid <n>`, `no security-object <n>` | Delete an SSID (its own security object only) | 🟡 |
| `clear auth station mac <aabb:ccdd:eeff>` (then `clear auth station <mac>`) | Disconnect a client | 🟡 least certain |
| `save config` | Persist changes (90 s timeout) | 🟡 |
| `reboot` | Reboot (any y/n prompt is answered `y`) | 🟡 |

SSID names with spaces are sent in double quotes.

## SR switch CLI (SR2208P, software 1.0.1.28)

### Read

| Command | Used for | Status |
| --- | --- | --- |
| `enable`, `terminal length 0` | Privileged mode, no paging (an in-shell `User:` / `Password:` prompt is answered) | ✅ |
| `show version` | Model, firmware, serial, MAC | ✅ |
| `show network` | IP, netmask, gateway | ✅ |
| `show port all` | Link, speed, duplex, admin state | ✅ |
| `show interface ethernet <port>` | Traffic counters → throughput | ✅ |
| `show poe` | PoE budget / consumed | ✅ |
| `show poe port info <port>` | Per-port PoE power, status, class, voltage, current, temperature | ✅ |
| `show mac-addr-table` | Clients per port | ✅ |
| `show lldp remote-device all` + `… detail <port>` | LLDP neighbours, their IP and capabilities | ✅ |
| `show running-config` | Uptime, port / PoE shutdown state | ✅ |

### Change

| Command | Used for | Status |
| --- | --- | --- |
| `configure` → `interface <port>` → `poe` / `no poe` → `exit` → `exit` | PoE on / off | ✅ commands exist and are accepted |
| … → `poe reset` → … | PoE power cycle (no config change) | ✅ command exists |
| … → `shutdown` / `no shutdown` → … | Port on / off | ✅ command exists |
| `write memory` | Save (any y/n prompt is answered `y`) | 🟡 confirmation text not captured |
| `reload` | Reboot | 🟡 |

The session recovers from the switch's idle timeout by logging in again, re-entering
`enable` and re-running the command.

## Older HiveOS-style SR switch firmware (`AH-xxxxxx#` prompt)

`show pse`, `show mac-address-table`, `interface ethN/N pse shutdown`: 🟡 guessed, untested.
