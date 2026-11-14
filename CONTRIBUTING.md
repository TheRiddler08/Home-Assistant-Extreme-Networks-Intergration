# Contributing

Thanks for helping! The most valuable contribution is **real device output** from models or
firmware versions that aren't in [Supported devices](docs/supported-devices.md) yet.

## Reporting a device (no coding needed)

1. In Home Assistant, go to **Developer tools → Actions** and run **Aerohive Local: Run show
   command** for the commands the
   [device support form](https://github.com/TheRiddler08/Home-Assistant-Extreme-Networks-Intergration/issues/new/choose)
   asks for. Passwords and keys are masked automatically.
2. Remove serial numbers and anything else you consider private (you can replace the last
   digits of MAC addresses).
3. Open a device support request and paste the output.

## Code changes

1. Fork the repo and create a branch.
2. Install the test requirements and run the tests:

   ```bash
   pip install -r requirements_test.txt
   ```
   ```bash
   pytest
   ```

   Install `pytest-homeassistant-custom-component` to also run the Home Assistant tests.
3. Keep `parsers.py` and `commands.py` free of Home Assistant imports, and keep parsers
   tolerant: unexpected lines must never raise.
4. Add or update tests and fixtures (anonymised) for any parser change.
5. Never log, store or return passwords or Wi-Fi keys.
6. Update the docs (and [CHANGELOG.md](CHANGELOG.md)) if behaviour changes.
7. Open a pull request. The template lists the checks.

See [Development](docs/development.md) for the project layout and release process.
