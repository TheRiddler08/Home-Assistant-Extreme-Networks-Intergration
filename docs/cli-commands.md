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

## ExtremeXOS (Summit X450e and other EXOS switches)

All 🧪: written from the ExtremeXOS 12.x–16.x command reference (its example output is
what the parsers are tested against) and not yet confirmed on a device. Real output is very welcome (see [Supported devices](supported-devices.md)).

### Read

| Command | Used for |
| --- | --- |
| `disable clipaging` | No paging (the `Press <SPACE> to continue` pager is answered otherwise) |
| `show switch` | Model, system MAC, uptime, boot count, booted / selected image and configuration, state |
| `show ports no-refresh` | Port admin state (E/D), link (A/R/NP), speed, duplex, display string |
| `show ports statistics no-refresh` | Byte / packet counters → throughput (one command for all ports) |
| `show inline-power`, `show inline-power info ports` | PoE budget / measured; per-port state, class, volts, current, power, fault |
| `show fdb` | Clients per port |
| `show cpu-monitoring`, `show memory` | CPU (System 5 s), memory |
| `show configuration` | Backups and change detection (every 15 min and after changes) |
| `show lldp neighbors` + `show lldp neighbors detailed` | LLDP neighbours, their IP, capabilities and LLDP-MED model (every 15 min; one command for all ports) |
| `show version` | Serial number (every 15 min) |
| `show vlan` | VLANs and the management IP address (every 15 min) |
| `show temperature`, `show fans`, `show power` | Environment sensors (every 15 min) |

The prompt (`X450e-48p.3 #`, or `Slot-1 Stack.3 #` on a stack) is recognised automatically;
a leading `*` means unsaved changes.

### Change

| Command | Used for |
| --- | --- |
| `enable ports <n>` / `disable ports <n>` | Port on / off |
| `enable inline-power ports <n>` / `disable inline-power ports <n>` | PoE on / off |
| `reset inline-power ports <n>` (fallback: disable, wait 5 s, enable) | PoE power cycle |
| `configure ports <n> display-string <name>` / `unconfigure ports <n> display-string` | Port name |
| `save configuration` | Save (the y/N question is answered `y`) |
| `reboot` | Reboot. With unsaved changes ExtremeXOS asks "save and reboot?" (`y` / `n`): it's answered as the `save_changes` option says, and the reboot is refused without it |

## OpenWrt

All 🧪 (simulated router, OpenWrt 23.05-style output). `ubus` JSON and UCI; see the full list in
[OpenWrt → Commands used](openwrt.md#commands-used).

## Configuration backups (all devices)

`show running-config` (HiveOS, SR switches) or `show configuration` (ExtremeXOS) is read to
take backups. A restore sends the backup's lines back one by one; see [backups](backups.md).

## Older HiveOS-style SR switch firmware (`AH-xxxxxx#` prompt)

`show pse`, `show mac-address-table`, `interface ethN/N pse shutdown`: 🟡 guessed, untested.
