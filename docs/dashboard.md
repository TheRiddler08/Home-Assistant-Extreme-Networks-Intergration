# Network panel and dashboard cards

The integration adds a **Network** page to the Home Assistant sidebar and three dashboard
cards. Nothing needs installing or configuring: they appear once the integration is loaded.
(Screenshots use demo data.)

## The Network panel

![Network panel overview](images/panel-overview.png)

**Devices** shows every access point, router and switch with its status, a small view of the
switch ports, and any problems: offline, unsaved changes, configuration changed after a
reboot, boot problems, hardware faults. The totals at the top count online devices, Wi-Fi and
wired clients, PoE power and problems.

Select a device to open it.

### Switches

![Switch page](images/panel-switch.png)

- **Front panel:** the ports laid out like the switch, odd ports on top. The colour shows the
  link speed (green 1 Gbit/s, olive 100 Mbit/s, ochre 10 Mbit/s), grey means no link and
  stripes mean the port is turned off. A yellow dot marks a port delivering PoE, a blue arrow
  an uplink. Stack members get one row each.
- **Ports table:** status, what's connected (LLDP neighbour, or the device learned on the
  port), PoE power and traffic. Filter it to connected ports, PoE ports, or disabled ports and
  ports with errors.
- **Health:** uptime, CPU, memory, temperature, PoE budget, fans (with RPM) and power supplies.
- **VLANs** (ExtremeXOS), **Device** details and the last configuration backup.

Select a port, on the front panel or in the table, to manage it:

![Port dialog](images/panel-port-dialog.png)

- **Port on / off.** Turning off an uplink asks first, because it can cut Home Assistant off
  from the switch.
- **PoE on / off** and **Restart PoE device** (power-cycles a camera or access point).
- **Port name** (ExtremeXOS display string, up to 15 characters, no spaces).
- The LLDP neighbour with its address, PoE draw and class, traffic, and the devices learned
  on the port, named when an access point knows their hostname.

Changes are saved on the switch straight away, like every change made from Home Assistant.

### Access points

Radios (band, channel, width, power, utilisation, noise, clients, SSIDs), Wi-Fi networks and
the client list with signal strength. To add networks or change passwords, use the
[Wi-Fi manager](wifi.md).

### Alerts and actions

![Configuration alert](images/panel-alert.png)

Every device page has **Save configuration**, **Back up now** and **Reboot**. Alerts from
[power-loss protection](backups.md) show at the top with what to do:

- **Configuration changed after a reboot:** *Restore last backup* sends the last known-good
  configuration back and saves it; *Keep current* accepts the running configuration as the new
  known-good one.
- **Unsaved changes** (ExtremeXOS): *Save now*.
- **Reboot** on an ExtremeXOS switch with unsaved changes asks whether to save them first or
  reboot without them. It never decides for you.

### Clients

The **Clients** tab lists every Wi-Fi and wired client in one place. Search it by name, MAC
address, IP address, device or port. Select a wired client to open its switch port.

### Phones

The panel works at phone width; the front panel wraps into rows of 12 ports.

![Phone layout](images/panel-mobile.png)

## Dashboard cards

![Cards in dark mode](images/cards-dark.png)

Add them from the card picker (search for *Aerohive*) or in YAML. The `device` option takes
the device name, as shown in the Network panel.

### Switch card

A live front panel. Select a port to get the same port dialog as in the panel.

```yaml
type: custom:aerohive-switch-card
device: X450e-48p
legend: true          # optional: colour legend under the ports
title: Garage switch  # optional
```

### Device card

Health, radios, last backup and problems for one device, with Save (when there are unsaved
changes), Restore (after a configuration change after reboot) and Reboot buttons.

```yaml
type: custom:aerohive-device-card
device: AH-7c0680
actions: false        # optional: hide the buttons
```

### Network card

Every device with its status, plus client and problem totals.

```yaml
type: custom:aerohive-network-card
title: Network        # optional
```

The arrow in each card's header opens that device in the Network panel.

## Notes

- **Who can use it:** the panel is visible to every user, as the integration's entities are.
  To hide it for a user, long-press the sidebar title and remove *Network*.
- **Live updates:** the panel and cards update after each poll (the scan interval in the
  integration's options), and right after a change.
- **Browser cache:** after updating the integration, reload the page if the panel looks
  outdated. The module URL includes the version, so this is rarely needed.
- **Entities:** the panel doesn't replace them. Automations, history and other dashboard
  cards keep using the [entities](entities.md).
