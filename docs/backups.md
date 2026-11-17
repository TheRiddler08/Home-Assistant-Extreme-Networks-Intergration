# Power-loss protection and configuration backups

Network gear can come back from a power cut in a bad state, and fixing it by hand can take
hours. The usual causes are:

- **Unsaved changes are lost.** Changes made on the CLI or in a web UI live only in the
  running configuration until someone saves them. A power cut throws them away.
- **The device boots the wrong configuration or image.** A configuration file that was
  being written when power dropped can be corrupted, and an ExtremeXOS switch then comes up
  with a factory-default configuration (or the secondary image) instead of yours.
- **There's no copy to restore from.**

The integration protects against all three for every device it manages (APs, SR switches
and ExtremeXOS switches).

## What it does

| Protection | How |
| --- | --- |
| **Automatic configuration backups** | The full configuration (`show running-config`, or `show configuration` on ExtremeXOS) is read every 15 minutes and after every change made from Home Assistant. A new backup file is only written when the configuration actually changed; comments and timestamps are ignored. The last 20 backups per device are kept. |
| **"Configuration changed after reboot" alert** | If the device rebooted since the latest backup and its configuration now differs from it, that's the power-loss symptom. The binary sensor `Configuration changed after reboot` turns on, and a **Repairs** notice explains what changed (lines added / removed). No new backup is taken while the alert is active, so the known-good one stays the latest. |
| **"Boot configuration problem" alert** (ExtremeXOS) | `show switch` is checked every poll. The alert turns on if the switch booted `Factory Default`, booted a different configuration or image than the selected one, or isn't `OPERATIONAL`. A Repairs notice is raised too. |
| **"Unsaved configuration changes"** (ExtremeXOS) | ExtremeXOS marks unsaved changes with a `*` in the prompt. The binary sensor turns on while there are unsaved changes, made from anywhere. |
| **Save configuration button** | Saves the running configuration on the device (`save config`, `write memory` or `save configuration`). |
| **Network-wide view** | The *Aerohive Network* device has a `Devices with configuration problems` sensor listing every device with an alert, unsaved changes, or that's offline. |

Changes made from Home Assistant are always saved on the device immediately, so they're
never the ones lost in a power cut.

## When an alert appears

The device's page in the [Network panel](dashboard.md#alerts-and-actions) shows the alert with
**Restore last backup** and **Keep current** buttons. **Settings → System → Repairs** also
shows a notice such as *"X450e-48p: configuration changed
after a reboot"*, with the lines that differ. Then either:

- **The backup is right** (the change is the damage): run the **Restore configuration**
  action, see [below](#restoring-a-backup). The alert clears once the device reports the
  restored configuration.
- **The current configuration is right** (for example, someone changed and saved it before
  the reboot): press **Accept current configuration** on the device page. That saves it as
  the new known-good backup and clears the alert.

## Where backups are stored

`/config/aerohive_local_backups/<device name>_<MAC>/YYYYmmdd-HHMMSS.cfg` (times in UTC),
inside your Home Assistant configuration folder. They're plain text, so you can open them
with the File editor or Studio Code Server add-ons. They're included in Home Assistant's
own backups, so they survive a Home Assistant reinstall too.

The `Last configuration backup` sensor shows when the latest backup was taken, its file name
and how many backups exist. The **List configuration backups** action
(`aerohive_local.list_backups`) returns the list.

> **Keep these files private.** They contain your full device configuration. On
> ExtremeXOS and SR switches that can include encrypted passwords and SNMP communities.

## Restoring a backup

**Developer tools → Actions → Aerohive Local: Restore configuration**:

```yaml
action: aerohive_local.restore_config
data:
  device_id: <your switch>
  confirm: true
  save: true                     # save on the device afterwards
  # file: 20261114-160000.cfg    # optional, default: the latest backup
```

The backup is sent to the device line by line, like typing it on the CLI. The response
lists how many lines were sent and which ones the device rejected. Rejections such as
"already exists" are normal when restoring over a configuration that's mostly intact; the
remaining lines are still applied. Nothing is saved on the device unless `save: true`, so a
reboot undoes a restore that went wrong.

Limits:

- **Wi-Fi keys on APs can't be restored.** HiveOS shows them masked (`ascii-key ***`). Lines
  with masked secrets are skipped, so set those keys again with the [Wi-Fi manager](wifi.md).
- **A restore that changes the management IP or VLAN** can cut Home Assistant off half-way.
  For those, restore from the device's console instead.
- **It's an overlay, not a replace.** Settings that exist on the device but not in the backup
  are left alone. To get back to exactly the backup, reset the device to defaults first (on
  its console), then restore.

## Recovering an ExtremeXOS switch by hand

If the switch booted `Factory Default` or the wrong image, the integration may not be able to
log in at all (the default configuration has no SSH user). On the switch's console:

```text
show switch
```

Check *Image Selected / Booted* and *Config Selected / Booted*. To boot your saved
configuration and image again:

```text
use configuration primary
```
```text
use image primary
```
```text
reboot
```

If `primary.cfg` itself is corrupted, copy a backup file from Home Assistant to the switch
(for example over TFTP with `tftp get <server> <file> primary.xsf`), then run
`load script primary.xsf` and `save configuration`. See the ExtremeXOS user guide for your
release.

## Automation ideas

**Save every device when the UPS switches to battery**, so nothing unsaved is lost if the
outage outlasts the battery:

```yaml
automation:
  - alias: Save network configuration on power failure
    triggers:
      - trigger: state
        entity_id: sensor.ups_status        # your UPS integration's status sensor
        to: "On Battery"
    actions:
      - action: button.press
        target:
          entity_id:
            - button.x450e_48p_save_configuration
            - button.ah_switch_save_configuration
            - button.ah_7c0680_save_configuration
```

**Get a phone notification when a device comes back wrong:**

```yaml
automation:
  - alias: Network configuration problem
    triggers:
      - trigger: state
        entity_id: sensor.aerohive_network_devices_with_configuration_problems
    conditions:
      - condition: numeric_state
        entity_id: sensor.aerohive_network_devices_with_configuration_problems
        above: 0
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: Network configuration problem
          message: >-
            {% for d in state_attr('sensor.aerohive_network_devices_with_configuration_problems', 'devices') %}
            {{ d.name }}: {{ d.problems | join(', ') }}
            {% endfor %}
```

**Remind yourself about unsaved changes** (ExtremeXOS): trigger on
`binary_sensor.<switch>_unsaved_configuration_changes` being `on` for 30 minutes.

Entity IDs depend on your device names: check them on each device's page.
