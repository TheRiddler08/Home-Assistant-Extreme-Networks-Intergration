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

## Configuration backups

See [Power-loss protection and backups](backups.md) for the full picture.

| Action | Does |
| --- | --- |
| `aerohive_local.save_config` | Save the running configuration on the device |
| `aerohive_local.backup_config` | Back up now (returns the file; no duplicate if unchanged). Refused while a "changed after reboot" alert is active |
| `aerohive_local.list_backups` | List the backups of a device (returns a response) |
| `aerohive_local.accept_config` | Use the current configuration as the new known-good backup and clear a "changed after reboot" alert (same as the Accept button) |
| `aerohive_local.restore_config` | Send a backup back to the device (`confirm: true` required; `save: true` to save afterwards; `file:` to pick one) |
