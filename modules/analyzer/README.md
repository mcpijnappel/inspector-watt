# Logic analyzer module

Decodes I2C and SPI on the board's pins next to the current, with sigrok (issue #12). One container.

Works with FX2-based analyzers (nanoDLA, the 8-channel 24 MHz "Saleae clones"): sigrok loads its open firmware
(fx2lafw) into them at every connect.

Install:

1. The udev rule: `sudo cp modules/analyzer/70-inspector-watt-analyzer.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger`
2. In `.env`: `COMPOSE_PROFILES=analyzer`, then `./dc up -d`.
3. Settings → Modules shows whether it found the analyzer.

The simulator uses sigrok's demo device (`ANALYZER_DRIVER=demo`).
