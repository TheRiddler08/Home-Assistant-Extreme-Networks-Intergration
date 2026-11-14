# Development

## Project layout

```
custom_components/aerohive_local/
  client.py         asyncssh session: login prompts, enable, paging, prompt detection, retries
  commands.py       CLI command builders + input validation (no Home Assistant imports)
  parsers.py        pure parsers for every show command (no Home Assistant imports)
  discovery.py      network scan + MAC prefix / hostname matching
  coordinator.py    one DataUpdateCoordinator per device; HiveOS and switch-CLI polling; actions
  overview.py       "Aerohive Network" overview entry (aggregates all devices)
  config_flow.py    setup (scan / manual / DHCP / LLDP discovery) + options (Wi-Fi manager, settings)
  sensor.py, binary_sensor.py, switch.py, number.py, text.py, button.py, device_tracker.py
  diagnostics.py    redacted diagnostics download
tests/
  fixtures/         device output (real ones are named after the device, synthetic_* are not real)
  test_parsers.py, test_commands.py, test_discovery.py   pure unit tests
  test_client.py    fake legacy-algorithm SSH servers (HiveOS and SR switch CLI)
  test_init.py, test_discovery_and_switch.py            Home Assistant integration tests
```

## Running the tests

```bash
pip install -r requirements_test.txt
```
```bash
pytest
```

The parser, command, discovery and SSH-client tests run without Home Assistant. The
SSH-client tests start fake SSH servers on localhost that only offer the legacy algorithms,
with paging, login prompts, idle timeouts and confirmations.

The Home Assistant tests (`test_init.py`, `test_discovery_and_switch.py`) run when
`pytest-homeassistant-custom-component` is installed:

```bash
pip install pytest-homeassistant-custom-component
```

They cover the config, options and Wi-Fi manager flows, entity setup, controls,
debouncing, entity cleanup, the overview, services and diagnostics.

## Adding support for a new device or firmware

1. Capture output with the `aerohive_local.run_show_command` action (see
   [Actions](actions.md)), or ask the reporter to attach it to a
   [device support issue](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose).
2. Anonymise it (serial numbers, MAC suffixes, hostnames, internal names), then save it as
   `tests/fixtures/<model>_<command>.txt`.
3. Add parser tests against the fixture, then adjust `parsers.py` (keep it tolerant: unknown
   lines must never raise).
4. Update [Supported devices](supported-devices.md) and [CLI commands](cli-commands.md).

## Releases

HACS installs GitHub releases, so Home Assistant shows updates in **Settings → Updates**.

To publish a release:

1. Bump `version` in `custom_components/aerohive_local/manifest.json` (e.g. `0.6.2`).
2. Add an entry to [CHANGELOG.md](../CHANGELOG.md).
3. Push to the default branch.

The **Release** workflow publishes `v0.6.2` with generated notes if that release doesn't
exist yet. Pushing a matching tag, or running the workflow by hand (Actions → Release → Run
workflow), works too.

## Getting listed in HACS

Being in the HACS default store means users find the integration by searching HACS,
without adding it as a custom repository. Checklist:

1. **Repository settings** (GitHub → Settings → General): a description, topics
   (`home-assistant`, `hacs`, `hacs-integration`, `homeassistant-custom-component`,
   `aerohive`, `extreme-networks`), Issues enabled, and `main` as the default branch.
2. **At least one GitHub release** (done by the Release workflow).
3. **Brand icon:** open a pull request to
   [home-assistant/brands](https://github.com/home-assistant/brands) adding
   `custom_integrations/aerohive_local/icon.png` and `icon@2x.png` from [`brand/`](../brand).
   Follow that repository's README for the current image rules.
4. **Validation passes:** once the brands PR is merged, remove `ignore: brands` from
   `.github/workflows/validate.yml`, and make sure the *Validate* workflow (HACS action and
   hassfest) is green.
5. **Submit:** open a pull request to [hacs/default](https://github.com/hacs/default) adding
   `TheRiddler08/Home-Assistant-Extreme-Networks-Intergration` to the `integration` file
   (alphabetical order). Fill in its pull request template. The HACS bot runs the same checks.
