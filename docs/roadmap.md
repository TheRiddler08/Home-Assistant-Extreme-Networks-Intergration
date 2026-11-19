# Roadmap and open items

What's planned, what's on hold, and what each open item needs before it can be finished.
Newest decisions first.

## On hold

### Dual WAN (Starlink failover / load balancing)

**On hold** (decided 2026-11-19). The plan: OpenWrt with `mwan3` balances new connections
across the main ISP and Starlink while both are healthy and fails over when one drops.
Nothing for it is in the integration yet. To pick it up again: decide balance vs. failover
only, how health is checked (ping targets, interval), and whether the Setup tab should
generate the `mwan3` configuration (interfaces, members, policies, rules) with the same
rollback as the rest of the network setup.

## Open: Aerohive access points in the network setup

The Setup tab already sends Aerohive APs their Wi-Fi networks, VLAN (user profile),
password, bands, hidden name and max clients. Three things are **not sent yet**. The access
points can do them, but the exact command wording on IQ Engine / HiveOS hasn't been confirmed
on a real device, and a wrong command is rejected and can leave a network half set up. Until
then these are skipped on Aerohive APs, with a note in **Review & apply**:

| Feature | Status on Aerohive | What's needed |
| --- | --- | --- |
| **Per-device keys (local PPSK)** | Supported by the APs: keys are stored on the AP, up to 9999 per AP, 1000 per group, each group with its own user profile (VLAN), 1–15 clients per key, instant disconnect with `clear auth roaming-cache mac <mac>` / `clear auth local-cache mac <mac>` ([Extreme's PPSK guide](https://documentation.extremenetworks.com/xiq/legacy/Aerohive_PPSK-Guide.pdf)). The guide only covers HiveManager; CLI wording seen so far is from unofficial notes (`ssid <name> user-group <group>`, `security-object <name> security private-psk …`). | Output of `show running-config` and `show version` from the AP460C (through the `aerohive_local.run_show_command` action; keys are hidden), ideally from an AP that once had a PPSK SSID. Or a read-only check of the CLI's built-in help for `security-object … security` and `user-group`. |
| **WPA3-only** | Expected on IQ Engine 10.x (AP460C runs 10.5). WPA2/WPA3 networks are sent as WPA2 for now. | The protocol-suite name for WPA3-SAE on this firmware (same sources as above). |
| **Client isolation** | Standard Aerohive feature (inter-station traffic). | The exact `ssid <name> …` option name on this firmware. |

When confirmed, each becomes a few lines in `netplan.hiveos_commands` plus tests with the real
output as a fixture (anonymised). Revoking a key should then also disconnect the device on
every AP (`clear auth roaming-cache mac <mac> hive-neighbors`).

## Open: smaller items

- **SR2208P VLANs** in the Setup tab (FASTPATH `vlan database` / `vlan participation`):
  needs the switch's VLAN commands confirmed.
- **Key rotation** with old/new overlap and reminders (the plan's six-month Google
  password rotation).
- **Onboarding portal phase 2:** device-type rules (e.g. TVs always to homeISO), approve from
  a phone notification, a request limit per hour.

## Done

- 0.14.0: QR codes for keys and Wi-Fi passwords; onboarding portal phase 1 (served by the
  router, requests approved in Home Assistant, the device shown its key and QR code).
- 0.13.0: network setup (networks, access grid, Wi-Fi, per-device keys, review & apply with
  rollback on OpenWrt).
