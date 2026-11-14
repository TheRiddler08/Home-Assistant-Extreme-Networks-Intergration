# How it works

- **AP and broadcasting detection:** a device counts as an AP if it has any `wifiN` radio,
  SSIDs or clients (even when every radio is down). An SSID counts as bound to a radio if the
  `show interface` table lists it, or if the running config has `interface wifiN ssid <name>`.
  Names the table cuts short are matched to their full `show ssid` names. It counts as
  broadcasting while that radio is up; a channel shown as `Auto` / DFS scanning still counts.
- **Glitch protection:** a `show interface` reply without `mgt0`, or missing the radios the
  last poll had, is retried once; if it's still incomplete, that poll fails and the last good
  data is kept, instead of the AP suddenly showing no Wi-Fi. If SSIDs are bound but
  `show ssid` gives nothing usable, the last list is kept. A command is only treated as
  unsupported after being rejected on two polls in a row, and it's tried again every 30 minutes.

- **One SSH session per device**: a persistent interactive shell is reused, and every
  command goes through a lock. There is at least 0.3 s between commands, and the session
  reconnects (with one retry) if it drops.
- **Paging**: on connect the integration sends `console page 0`. If the device rejects it,
  `--More--` prompts are answered with a space.
- **Error detection**: if the output of a config command contains `ERROR`, `Invalid`,
  `unknown keyword`, `Incomplete command` or `Ambiguous command`, the integration raises a
  `HomeAssistantError` carrying the device's message, and skips `save config`.
- **Debouncing**: changes to a switch or number within 1.5 s are coalesced. Only the final
  value is sent, and nothing is sent if the device already has it.
- **Secrets**: passwords and Wi-Fi keys are never logged. Diagnostics redact the host,
  username, password, key path, tracked MACs and client MAC/IP/hostnames.
- **Optional commands**: an optional command (`show station`, `show interface wifiN`,
  `show boot-param`, …) is skipped once it has been rejected on two polls in a row, and
  tried again every 30 minutes.
