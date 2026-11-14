# Security policy

This integration stores device credentials in Home Assistant and logs in to your network
equipment over SSH. Please report security problems privately.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting**: open the repository's **Security** tab,
then **Report a vulnerability**. Please don't open a public issue for security problems.

## What the integration does to protect you

- Passwords and Wi-Fi keys are never written to logs, error messages, entity states or
  diagnostics. The debug `run_show_command` action masks `ascii-key` / `password` arguments
  by default.
- Only single-line `show` commands are accepted by the debug action. Every value sent to the
  CLI is validated, so it can't inject extra commands.
- SSH host keys are not verified (`known_hosts=None`), which is common for LAN appliances.
  Only use the integration on networks you trust.
- The devices only support legacy SSH algorithms (SHA-1 key exchange and MACs, CBC
  ciphers). Keep device management on a trusted network or management VLAN.

## When posting device output

Remove serial numbers, MAC addresses (or their last digits), hostnames, IP addresses and
any keys before attaching output to an issue.
