# UniFi access points and switches (preview)

Ubiquiti UniFi devices are managed by a **UniFi Network controller**, so Home Assistant talks
to the controller's local API, not to the devices. Supported controllers: UniFi OS consoles
(Cloud Key, UDM, UDR, UCG, UniFi OS Server) and the self-hosted Network application.

> 🧪 **Preview.** Built from the controller's web API as its own web page uses it, and tested
> against responses of that shape; not yet confirmed on a real controller.

## Adding them

1. On the controller, make a **local account** for Home Assistant (UniFi OS: *Admins & Users →
   Create New → Restrict to local access only*). A Ubiquiti cloud account with two-factor
   sign-in can't be used. *View only* is enough to watch; changing ports, PoE and Wi-Fi needs
   *Site Admin* for the Network application.
2. In Home Assistant: **Settings → Devices & services → Add integration → Aerohive, Extreme
   Networks & OpenWrt (Local SSH) → UniFi access points and switches (UniFi controller)**.
3. Enter the controller's address (port **443** for UniFi OS consoles, usually **8443** for the
   self-hosted application), the account, and the **site** (`default` unless you made others).
4. Choose which access points and switches to add. Each becomes its own device. Gateways
   (UDM/UXG as a router) aren't offered yet.

All devices of one controller share one login and one read of the controller per poll.

## What you get

| | Access points | Switches |
| --- | --- | --- |
| Device | model, firmware, uptime, CPU, memory, IP, MAC | the same, plus temperature and PoE budget |
| Ports | the uplink port | every port: link, speed, duplex, traffic, errors, uplink, SFP |
| PoE | | per port: on/off, power, class, delivering; **power-cycle** |
| Wi-Fi | radios (band, channel, width, power, clients), the Wi-Fi networks on each radio | |
| Clients | Wi-Fi clients with signal, rates, connected time, IP and name | wired clients on their port, with VLAN |

## Controls

- **Ports on/off** (switches): sets the port to the controller's *Disabled* setting
  (`forward: disabled` in the device's port overrides); turning it on returns it to its port
  profile. The uplink needs `force`.
- **PoE on/off** (`poe_mode` off/auto) and **power-cycle** (`cmd/devmgr power-cycle`).
- **Wi-Fi networks:** on/off, password and hidden name (`rest/wlanconf`). A UniFi Wi-Fi network
  is on or off for every access point at once, so the per-radio switches don't apply. Adding or
  removing networks and the client limit stay in the controller for now.
- **Disconnect a client** (`cmd/stamgr kick-sta`) and **restart** a device.

## Not (yet) with UniFi

- The **network setup** (Setup tab) doesn't change UniFi devices yet. Networks (VLANs),
  Wi-Fi networks with per-device keys (UniFi supports them as *private pre-shared keys*) and
  switch port profiles are the next step; see the [roadmap](roadmap.md).
- **Port speed**, **backups** (the controller keeps the configuration and its own backups),
  UniFi gateways, and `run_show_command`.
