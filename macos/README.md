# Menu bar icon

A small macOS menu bar app for Transcribator. AppKit and Foundation only, no third-party dependencies. The UI is in English, or in Russian when Russian is the first preferred system language.

What it does:

- shows the service state: "Running · N queued · N processing", "Starting…", "Stopped". If the service responds but the LaunchAgent is not loaded (started by hand, e.g. `uv run transcribator serve`), it shows "Running (started manually)" and the Start/Stop item is disabled;
- opens the web UI at `http://127.0.0.1:8765`;
- stops and starts the service (LaunchAgent `local.transcribator`) via `/bin/launchctl`;
- turns start at login on and off;
- "Quit icon" closes only the icon, the service keeps running.

Privacy: the app talks only to `http://127.0.0.1:8765/api/status` (counters only), with no cache, cookies, proxy or redirects, and runs only `/bin/launchctl`. It logs nothing and has no telemetry. `Info.plist` allows local networking only (`NSAllowsLocalNetworking`).

## Build

Requires the Command Line Tools (`xcode-select --install`), Xcode is not needed.

```sh
scripts/build_menubar.sh    # builds the app and installs it into ~/Applications
```

The install path is fixed: `transcribator autostart install` expects the app there. The script replaces any previous copy.

The app is ad-hoc signed and meant to run on this Mac only. Install the service autostart first: `uv run transcribator autostart install`. A separate LaunchAgent `local.transcribator.menubar`, installed by the same command, starts the icon at login.
