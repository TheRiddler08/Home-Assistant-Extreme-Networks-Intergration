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
  command goes through a lock. There is at least 0.3 s between commands on access points and
  routers (0.05 s on switches), and the session reconnects (with one retry) if it drops.
- **Paging**: on connect the integration sends `console page 0`. If the device rejects it,
  `--More--` prompts are answered with a space.
- **Error detection**: if the output of a config command contains `ERROR`, `Invalid`,
  `unknown keyword`, `Incomplete command` or `Ambiguous command`, the integration raises a
  `HomeAssistantError` carrying the device's message, and skips `save config`.
- **Debouncing**: changes to a switch or number within 0.5 s are coalesced. Only the final
  value is sent, and nothing is sent if the device already has it.
- **Instant feedback**: once the device accepts a change (port, PoE, SSID on/off, hidden),
  the new state shows straight away instead of after the next poll. It's kept even if a
  poll that was already running returns the old state; the next poll shows what the device
  really reports.
- **Background save**: the configuration is saved on the device 2 seconds after the last
  change, so a burst of changes costs one save and the controls stay responsive. A failed
  save is retried on every poll and shows as a problem in the Network panel. A pending save
  is done before a reboot and when the integration unloads.
- **Device type**: the login prompt decides which CLI the device speaks. If `show version`
  then says otherwise (for example an ExtremeXOS switch with an unusual prompt), the
  integration switches to the right one. A device whose replies match none of them is
  shown as *not recognised* instead of as an access point.
- **Secrets**: passwords and Wi-Fi keys are never logged. Diagnostics redact the host,
  username, password, key path, tracked MACs and client MAC/IP/hostnames.
- **Optional commands**: an optional command (`show station`, `show interface wifiN`,
  `show boot-param`, …) is skipped once it has been rejected on two polls in a row, and
  tried again every 30 minutes.
