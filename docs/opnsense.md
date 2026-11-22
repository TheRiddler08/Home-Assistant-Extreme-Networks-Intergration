# OPNsense routers (preview)

OPNsense is read through its **REST API** with an API key, not over SSH. Nothing is installed
on the router. (Screenshot uses demo data.)

![OPNsense router](images/opnsense-router.png)

## Adding it

1. In OPNsense: **System → Access → Users**, edit a user (or create one for Home Assistant),
   and under **API keys** press **+**. Your browser downloads a file with the `key` and
   `secret`.
   - To monitor only, the user needs read access to the dashboard, diagnostics, interfaces
     and gateways (e.g. the *Dashboard*, *Diagnostics: …*, *Status: Interfaces* and
     *Status: Gateways* privileges). Reboot from Home Assistant needs a user that may
     reboot (e.g. *System: Firmware*, or full access).
2. In Home Assistant: **Settings → Devices & services → Add integration → Aerohive /
   Extreme Networks (Local SSH) → OPNsense router (API key)**.
3. Enter the router's address, HTTPS port (443 by default), the key and the secret. Leave
   **Check the HTTPS certificate** off for OPNsense's own self-signed certificate; turn it on
   if the router has a trusted certificate.

If the key is later deleted or changed, Home Assistant asks for a new one (a repair).

## What's read

| | API endpoint |
| --- | --- |
| Hostname, version | `diagnostics/system/system_information` |
| CPU, uptime, load | `diagnostics/activity/get_activity` (top's header) |
| Memory | `diagnostics/system/system_resources` |
| Temperature (hottest CPU sensor) | `diagnostics/system/system_temperature` |
| Physical ports: link, speed, duplex, MAC, assignment (WAN/LAN/…), traffic, errors | `interfaces/overview/interfaces_info/1` |
| Networks (assigned interfaces with an address, VLAN tag) | same |
| Internet gateways: online, delay, loss | `routes/gateway/status` |
| Devices behind the router: IP and hostname | `diagnostics/interface/get_arp` and DHCP leases from Kea (`kea/leases4/search`), Dnsmasq (`dnsmasq/leases/search`) or ISC (`dhcpv4/leases/search_lease`), whichever is in use |

Endpoints the router doesn't have (an older version, a feature not in use) are skipped: that
value is just unknown.

In the Network panel the router shows its ports (WAN marked as uplink), **Internet
gateways** with their status (a gateway that's down is listed as a problem), **Networks**,
health and device details. Devices behind it appear in the **Clients** tab with the names
and addresses from ARP and DHCP (see [client details](dashboard.md#clients)).

**Reboot** works from the device page and the `aerohive_local.reboot` action (it calls
`core/system/reboot`).

## Not (yet) on OPNsense

- **Port on/off, speed, PoE:** ports are read-only.
- **Configuration backups and change-after-reboot alerts:** OPNsense's configuration
  (`config.xml`) holds certificates, keys and password hashes, so it isn't copied into Home
  Assistant. OPNsense keeps its own configuration history (System → Configuration → History).
- **Network setup** (the Setup tab's router role: VLANs, addresses, DHCP, firewall rules):
  planned. Current OPNsense has the APIs for it, including interface assignment, but no
  automatic rollback through the API, so it needs care. See the [roadmap](roadmap.md).
- `run_show_command` and other SSH-only actions.

Built from the API controllers in OPNsense's source (opnsense/core, November 2026) and tested
against responses of that shape; not yet confirmed on a real router.
