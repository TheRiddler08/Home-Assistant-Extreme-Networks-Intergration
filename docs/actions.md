# Actions (services)

## `aerohive_local.run_show_command` (debug, returns a response)

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

## `aerohive_local.set_ssid_password`

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

## `aerohive_local.create_ssid` / `aerohive_local.delete_ssid`

```yaml
action: aerohive_local.create_ssid
data:
  device_id: <AP>
  ssid: Guest
  password: "guest-password"   # optional, open network if omitted
  radios: [wifi0, wifi1]       # optional, default: all active radios
```

This creates a security-object and an SSID with the same name, binds the SSID to the
radios and saves. `delete_ssid` unbinds the SSID from every radio, removes it, and also
removes its security-object if that object has the same name (i.e. it was created this way).

## `aerohive_local.power_cycle_poe`

```yaml
action: aerohive_local.power_cycle_poe
data:
  device_id: <switch>
  port: eth1/3
```

## `aerohive_local.disconnect_client`

```yaml
action: aerohive_local.disconnect_client
data:
  device_id: <AP>
  mac: "aa:bb:cc:dd:ee:ff"
```

## `aerohive_local.set_port`

Turn an Ethernet port on or off (and save). Uplink ports are protected unless `force: true`.

```yaml
action: aerohive_local.set_port
data:
  device_id: <switch>
  port: "5"          # 1/0/5 on SR switches, 5 on ExtremeXOS
  enabled: false
```

## `aerohive_local.set_ssid`

Change a Wi-Fi network on every access point that has it (or on one, with `device_id`).
All fields except `ssid` are optional.

```yaml
action: aerohive_local.set_ssid
data:
  ssid: Guest Wifi
  enabled: false           # on (on bands, or all radios) / off
  bands: ["2.4 GHz"]       # which bands it's on
  hidden: true
  max_clients: 20
  duration: "01:30:00"     # with enabled: only for this long, then back to how it was
```

A timed change is remembered across restarts. Turning the network on or off without a
duration cancels a running timer. Passwords are changed with `set_ssid_password`.

## `aerohive_local.refresh`

Read everything from a device right away, including what's normally read every 15
minutes (LLDP neighbours, temperature, fans, VLANs, the configuration). Leave out
`device_id` to refresh every device.

```yaml
action: aerohive_local.refresh
data:
  device_id: <switch>    # optional
```

## `aerohive_local.set_port_speed`

Set a port's speed and duplex, or back to auto-negotiation.

| Field | |
| --- | --- |
| `device_id` | The switch, router or access point. |
| `port` | As shown in the panel: `5` (ExtremeXOS), `1/0/3` (SR switch), `eth1` (HiveOS). |
| `speed` | `auto`, `10`, `100`, `1000` or `10000` (Mbit/s), as the port supports. |
| `duplex` | `full` or `half` for 10/100 Mbit/s. Default `full` (`auto` with Auto speed). |
| `force` | Also change an uplink port (default false). Going back to `auto` is always allowed. |

```yaml
action: aerohive_local.set_port_speed
data:
  device_id: 1234abcd...
  port: "9"
  speed: "100"
  duplex: full
```

## `aerohive_local.set_poe`

Turn PoE on or off for one port (and save).

```yaml
action: aerohive_local.set_poe
data:
  device_id: <switch>
  port: "12"
  enabled: false
```

## `aerohive_local.set_port_description` (ExtremeXOS)

Name a port (the display string shown in `show ports`): up to 15 letters, digits, `-`, `_`
or `.`, no spaces, and not just a number. An empty name clears it.

```yaml
action: aerohive_local.set_port_description
data:
  device_id: <switch>
  port: "12"
  description: camera-yard
```

## `aerohive_local.reboot`

Reboot a device. `confirm: true` is required.

On an ExtremeXOS switch with unsaved changes the reboot is **refused** unless you say what
to do with them: `save_changes: true` saves first, `save_changes: false` reboots without
them. The Reboot button refuses in that case too, so a reboot never throws changes away by
accident. On other devices `save_changes: true` saves before rebooting.

```yaml
action: aerohive_local.reboot
data:
  device_id: <switch>
  confirm: true
  save_changes: true
```

## Onboarding portal

### `aerohive_local.approve_device`

Approve a device waiting on the [onboarding portal](onboarding.md): it gets its own Wi-Fi key
and the portal shows it the password and QR code. Administrators only (automations may call
it).

| Field | |
| --- | --- |
| `request_id` | Required. The `id` from the `aerohive_local_onboarding_request` event. |
| `wifi` | ID of a Wi-Fi network with per-device keys. Default: the kind's rule, what the device asked for, or the first offered network. |
| `network` | ID of the network (VLAN) it lands in. Default: the rule's, else the Wi-Fi's own. |
| `minutes` | Minutes until the key stops working. Default: permanent (or the rule's). |
| `bind_mac` | Tie the key to the device's MAC address (off by default; phones use private addresses). |

### `aerohive_local.decline_device`

Decline a waiting device; the portal tells it so. Field: `request_id`.

## Blocking devices

### `aerohive_local.block_client`

Block a device on the router chosen in the [network setup](network-setup.md) (OpenWrt or
OPNsense), for good or as a pause. Administrators only (automations may call it), e.g. pause
the kids' tablets at bedtime.

| Field | |
| --- | --- |
| `mac` | Required. The device's MAC address. |
| `minutes` | A pause: minutes until it may go online again (1 to 43200). Empty: until unblocked. |
| `name` | Shown in the Clients tab and the activity list. |

```yaml
action: aerohive_local.block_client
data:
  mac: "aa:bb:cc:dd:ee:01"
  minutes: 600
  name: Kid's tablet
```

### `aerohive_local.unblock_client`

Let a blocked or paused device go online again. Field: `mac`.

## Configuration backups

See [Power-loss protection and backups](backups.md) for the full picture.

| Action | Does |
| --- | --- |
| `aerohive_local.save_config` | Save the running configuration on the device |
| `aerohive_local.backup_config` | Back up now (returns the file; no duplicate if unchanged). Refused while a "changed after reboot" alert is active |
| `aerohive_local.list_backups` | List the backups of a device (returns a response) |
| `aerohive_local.accept_config` | Use the current configuration as the new known-good backup and clear a "changed after reboot" alert (same as the Accept button) |
| `aerohive_local.restore_config` | Send a backup back to the device (`confirm: true` required; `save: true` to save afterwards; `file:` to pick one) |
