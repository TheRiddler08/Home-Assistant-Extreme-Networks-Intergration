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
