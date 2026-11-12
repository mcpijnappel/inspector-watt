# Logic analyzer module

Decodes I2C and SPI on the board's pins next to the current, with sigrok (issue #12). One container.

Works with FX2-based analyzers (nanoDLA, the 8-channel 24 MHz "Saleae clones"): sigrok loads its open firmware
(fx2lafw) into them at every connect.

Install:

1. The udev rule: `sudo cp modules/analyzer/70-inspector-watt-analyzer.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger`
2. In `.env`: `COMPOSE_PROFILES=analyzer`, then `./dc up -d`.
3. Settings → Modules shows whether it found the analyzer.

The simulator uses sigrok's demo device (`ANALYZER_DRIVER=demo`).

## In recordings

While a recording runs, the module decodes the bus by itself (Settings → Modules → Logic analyzer, *Decode in
recordings*): around each wake (default: *Before each wake* to *After each wake* past its end), all of it, or off.
It asks the api once a second which wakes capture found (`GET /api/modules/analyzer/recording`) and decodes each
window once it has passed; a long wake, and *All of it*, go in pieces of a few seconds. The messages are stored with
the recording, with a `bus_window` event per decoded stretch (or one saying why a stretch was missed). The session
page shows them in a Bus panel and as chip lanes under the current, Compare lines up both cycles' lanes, and agents
read them with the MCP tool `session_bus`. Only the decoded messages are kept, not the raw samples.
