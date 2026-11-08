<p align="center"><img src="docs/img/logo.svg" width="96" alt=""></p>

# Inspector Watt

**An AI detective for your firmware's power use, on the Nordic PPK2.**

Measure how much power a battery-powered sensor really uses, and why. A Nordic **PPK2** powers the
sensor and measures its current 100,000 times a second, while the sensor's debug **UART** (serial log) is
recorded on the same timeline. You see every wake-up next to the log lines that explain it, from a short
look to a week-long soak test.

And you don't have to do the detective work alone: AI agents such as Claude Code can read the same
measurements through MCP. With your permission they can also switch the sensor's power, record, and flash
new firmware from PlatformIO, so an agent can change the firmware, measure the result and try again.

![Live view: current and serial log](docs/img/live.png)

**What you can do**

- **Live:** watch the current and the serial log as they happen, switch the sensor's power and voltage,
  record, or save the last few minutes afterwards.
- **Sessions:** record in full detail or as a long, compact soak test. Zoom from days down to 10 µs,
  search the log and see unexpected gaps.
- **Phases and cycles:** log lines split each wake-up into phases (boot, measure, connect, upload, sleep),
  with the charge of each phase. Name kinds of cycles (measure only, upload...) and get the average of each.
  Print a few [plain log lines](docs/firmware-logging.md) and the Default profile finds them with no setup.
- **Battery life:** an estimate from the real mix of wake-ups, with what-ifs (another sleep time, a phase
  left out, upload every N wakes).
- **Compare:** two cycles on top of each other, from one session or two (e.g. before and after a
  firmware change), with per-phase differences and both serial logs.
- **Battery emulation:** the PPK2 behaves like a draining battery, to see what the sensor does when it
  runs low.
- **Agents:** AI agents such as Claude Code analyse your sessions through MCP, and with hardware control
  switched on (off by default, asked for every call) power-cycle, record and use the sensor's USB console.
- **Flashing:** `pio run -t upload` from any machine sends the build to the tracker, which flashes the
  sensor over its own USB while the PPK2 keeps powering it.

| Session | Compare |
|---|---|
| ![Session page](docs/img/session.png) | ![Compare two cycles](docs/img/compare.png) |

Screenshots: the built-in simulator.

---

- [Requirements](#requirements)
- [Install](#install)
- [Using it](#using-it)
- [Agents (MCP)](#agents-mcp)
- [Flashing from PlatformIO](#flashing-from-platformio)
- [Operations: update, backup, move, troubleshoot](#operations)
- [Development](#development)
- [Reference](#reference)
- [License](#license)

## Requirements

**Hardware**

- A **Nordic Power Profiler Kit II** (PPK2), used as the power supply ("source mode") for the sensor.
- A **USB-to-UART adapter** on the sensor's debug serial port (the udev rule expects a CH343; see
  [Install](#install) for another one).
- The sensor (DUT, "device under test"). On this bench: a Seeed XIAO ESP32-C6 powered through its battery
  pins (see [Hardware notes](#hardware-notes)).
- Optional: the sensor's **own USB port**, through an adapter that **blocks the 5 V** (data lines and ground
  only), for its USB console and [flashing from PlatformIO](#flashing-from-platformio). A third free USB port.

**The computer it runs on**

- **x86-64 Linux**, always on. A cheap mini PC is enough: Intel N100/N150 or better, **8 GB RAM**, an
  **SSD** (not eMMC, not an SD card), two free USB ports (no hub) and wired Ethernet. Measured on an
  N150: capture uses about 20 % of one core at 100 kS/s, all services together about 700 MB RAM.
- A Raspberry Pi 4/5 (4 GB, 64-bit OS, USB SSD) should work but is untested. A Pi 3 or Zero is too small.
- Disk: a detail recording takes about **0.7–0.8 GB per hour**; a 1-day soak about 0.5 GB.
- **Docker** with the compose plugin.

## Install

1. **Get the code** on the machine the PPK2 is plugged into:
   ```
   git clone https://github.com/mcpijnappel/inspector-watt.git && cd inspector-watt
   cp .env.example .env     # optional: settings, see Reference
   ```

2. **Docker**, if not installed yet:
   - Debian / Ubuntu: `curl -fsSL https://get.docker.com | sh`
   - Fedora: install `docker-ce` from Docker's repository (docs.docker.com/engine/install/fedora)
   - Bazzite / Fedora Atomic (layered):
     ```
     sudo curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo
     sudo rpm-ostree install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
     systemctl reboot
     grep -q '^docker:' /etc/group || grep '^docker:' /usr/lib/group | sudo tee -a /etc/group
     ```

   Then, on all of them:
   ```
   sudo systemctl enable --now docker
   sudo usermod -aG docker $USER      # log out and in again
   ```
   The containers run as user id 1000. If `id -u` shows something else, add `UID=` and `GID=` with your
   ids to `.env`.

3. **USB access and stable names** (`/dev/ppk2`, `/dev/dut-uart`, `/dev/dut-usb`) with the udev rule:
   ```
   sudo cp host-setup/70-inspector-watt.rules /etc/udev/rules.d/
   sudo udevadm control --reload && sudo udevadm trigger
   ```
   Replug both devices and check `ls -l /dev/ppk2 /dev/dut-uart`.
   - **Headless machine** (no one logged in at a desktop, e.g. a mini PC on the bench): the rule's
     `TAG+="uaccess"` only gives access to a desktop login. Replace it on each line with
     `OWNER="<your user name>"`.
   - For the sensor's own USB port the rule also tells ModemManager to leave it alone (its probing opens
     the port), and lets Linux suspend the link whenever the port is closed. Without traffic the firmware
     sees "not plugged"; otherwise Arduino's USB code counts a merely enumerated port as connected at boot,
     which keeps the sensor awake ~0.6 s longer per wake.
   - **Another USB-UART adapter:** look up its ids with
     `udevadm info /dev/ttyUSB0 | grep -E 'ID_(VENDOR|MODEL)_ID'` and change the `dut-uart` line.

4. **Start** everything, with the published images (amd64 and arm64):
   ```
   ./dc pull
   ./dc up -d
   ```
   Or build them from this checkout instead: `./dc up -d --build` (a few minutes on a mini PC).
   Open **http://localhost:8080**. Without the hardware, try the [simulator](#development) first.

5. **From a laptop or phone** (optional): set `WEB_BIND=0.0.0.0` in `.env` and run `./dc up -d` again, then
   open `http://<this machine>:8080`. By default the web UI only listens on this machine.

> **No login.** Anyone who can reach the web port can switch the sensor's power, flash firmware and read
> the upload and agent tokens in Settings. The tokens keep agents and scripts in line; they don't keep people
> out. Only open it up on a network you trust (or use an SSH tunnel).

Use `./dc` instead of `docker compose`: it always runs from the project's real path. (On Bazzite `/home`
links to `/var/home`; compose sees the two spellings as different projects and restarts containers.)

## Using it

### Live

The control strip at the top: **power** on/off and voltage, **Live / Pause**, **Record** and **Save...**.
The cards show the current now, the average of the visible window, a battery-life estimate and lost
samples. Drag across the chart to select a range; click a log line to put the cursor there. The log
filter **W+** shows warnings and errors, **E** only errors.

Not recording, but something interesting just happened? **Save...** stores the last 1, 2 or 5 minutes (or
a selected range) as a session: the live buffer keeps the last 5 minutes at full resolution.

The sensor is switched off by itself when nothing records and no browser has the app open for 10 minutes
(change it in Settings → Measurement & safety; 0 = never), so a forgotten board doesn't keep running.

### Recording

**Record** → pick a preset or set it up:

- **Detail:** every sample is kept (Parquet files, 1 per minute). About 0.7 GB/hour while the sensor is
  busy. Values are rounded to 12 significant bits (error ≤ 0.012 %); `RAW_MANTISSA_BITS=23` keeps them
  lossless (about 1.1 GB/h).
- **Soak:** for long runs. 10 ms summaries for the whole run; full-rate data only around each wake-up
  (current above the wake threshold, plus a margin before and after), optionally a rolling full-rate
  buffer of the last hours.

If the PPK2 drops out, stops sending (2 s watchdog) or the capture service restarts during a session, the
session continues: the missing time is stored as a **gap** (with the reason) and voltage and power are
restored. If the database is briefly unreachable, data waits in memory (up to 1 hour) or on disk
(`data/spool/`) and is written later.

### Sessions, phases and cycles

The session page shows the whole session at the top and a detailed chart with the serial log below. The
**Time** view scrolls through the session; the **Cycles** view steps from wake-up to wake-up.

**Phase rules** (button on the session page; kept as named profiles) turn log lines into phases. The
**Default** profile needs no setup when the firmware prints `Measuring`, `Connecting…`, `Uploading…` and
`Going to deep sleep…` at the start of those phases: see [Logging for Inspector Watt](docs/firmware-logging.md),
which you can also hand to a coding agent. A cycle
begins at each line that matches the first rule; the next rules mark where the next phases begin (text
or regular expression). A rule that doesn't match in a cycle is "not seen" there. Each phase gets its
duration, average current and charge; a boundary can snap to the nearest current step, since log lines
arrive 1–2 ms after the work they describe. The phase table unfolds a **spread** per phase over all cycles,
with the typical and unusual ones and links to compare them.

**Cycle types** (*Edit types...* in the cycle menu): not every wake-up does the same work, so a profile can
name kinds of cycles.

- A type **begins** at a phase and runs until the next begin, or until its **end** phase (stop before it,
  or include it: then it's a stretch of a cycle, such as Connect + upload).
- **Must have** / **Excluded** keep only the cycles with or without a phase.
- Examples (in the Default profile): *Measure only* = begin Boot, exclude Connect. *Upload to upload* =
  begin Connect (a period spanning several wake-ups).
- The cycle menu steps through one type and the phase table averages it. A cycle counts when it ended and
  its data is complete; it doesn't need every phase.

**Battery life** uses the real mix of wake-ups, with what-ifs: another sleep time, a phase left out, or a
type every N wakes.

### Compare

**Compare** (or *Compare...* on a session page) puts two cycles on top of each other: A in orange, B in
blue, from the same session or two different ones.

- The **type menu** next to the title picks the kind of cycle; both sides step through that type (‹ ›).
- **Align on** the cycle start, a phase, a current level or a log line; fine-tune B with − / + or the
  ← / → keys.
- **Show** the awake part, the whole cycle, one phase or a stretch.
- **These cycles / Session averages** switches between the two cycles and the averages of both sessions.
  The table shows the difference per phase.
- The serial logs below: **Merged** or **Side by side**, optionally only the lines that differ.

### Battery emulation

Record → *other settings* → Supply: **Battery emulation**. The PPK2 output follows a draining battery
instead of a fixed voltage.

- **Chemistries** (approximate rest-voltage curves per cell): LiPo (4.20 → 3.00 V), LiPo HV (4.35 V),
  LiFePO4 (flat 3.2–3.3 V), Li-SOCl2 (flat 3.6 V primary cell), alkaline and NiMH AA (e.g. Eneloop,
  IKEA LADDA; 2–4 cells). Packs above the voltage limit start at a lower charge; the form picks it.
- **Drain** by the measured charge (with a speed-up: 1000 mAh at 50× is empty after 20 mAh of real use), or
  a straight time sweep to the cutoff.
- **At the cutoff:** *protect* switches the sensor off and stops, like a protection circuit; *hunt* keeps
  lowering the voltage to a floor to find the brown-out, then stops.
- Sag under load isn't emulated (the PPK2 can't follow millisecond peaks); a small resistor in series
  (0.2–1 Ω) gives a real one. The session page shows the voltage over time.

### Settings

The gear icon in the top bar:

- **Devices:** the board on the bench, with its default voltage, a **maximum voltage that the capture
  service enforces** (never above `MAX_VOLTAGE_MV`), wake threshold, UART baud and phase rules. Sessions
  remember their device. Changing the device is refused while recording.
- **Phase profiles:** rename, edit rules and cycle types, duplicate, delete; record presets.
- **Measurement & safety:** idle power-off, spike filter (smooths the PPK2's range-switch spikes), battery
  sizes; the `.env`-only limits are shown read-only.
- **Storage:** disk space and data per session.
- **Agents (MCP):** see below.
- **System health:** services, PPK2 stream counters (lost samples, gaps, clock drift), UART, live buffer.

## Agents (MCP)

The `mcp` service lets AI agents (Claude Code, other MCP clients) list and analyse sessions, search the
serial log, summarise cycles, estimate battery life, look at the live measurement, read the DUT's USB console
and recent firmware uploads, and edit phase rules and cycle types. Hardware control is off unless you switch
it on (below). It never flashes firmware and can't see the upload token.

- This machine: `claude mcp add --transport http inspector-watt http://localhost:8300/mcp`
- Other machines on the network: switch it on in **Settings → Agents** and copy the command shown there
  (`... http://<tracker>:8080/mcp --header "Authorization: Bearer <token>"`).

Tools: `list_sessions`, `get_session`, `range_stats`, `current_over_time`, `search_log`, `list_events`,
`cycle_summary`, `get_cycle`, `battery_life`, `live_status`, `live_window`, `usb_console_lines`,
`list_uploads`, `list_profiles`, `test_phase_rules`, `test_cycle_types`, `save_profile`. Units: A, C, seconds
since the session start.

**Hardware control** (Settings → Agents, off by default) adds `dut_power`, `power_cycle`, `set_voltage`, `measuring`,
`start_recording`, `stop_recording`, `save_live_range`, `usb_console`, `usb_key` and `usb_send`. Three locks:

1. The switch. Off: the tools aren't listed and refuse to run; switching it off ends every allowed session.
2. A one-time allow per agent session. The first hardware call waits (up to 2 minutes) for a banner in the web
   UI: *Allow for this session* or *Deny*. The allow belongs to that MCP connection (its `Mcp-Session-Id`), so
   a new Claude Code session, `/mcp` reconnect or MCP service restart asks again. It ends after 8 h at the
   latest; *Agent control* in the top bar revokes it. A deny holds for 5 minutes.
3. Every hardware tool carries `_meta["anthropic/requiresUserInteraction"]`: Claude Code asks you before every
   call, in every permission mode (auto and bypassPermissions too), never offers "don't ask again", and
   refuses it in `dontAsk` / headless runs. Other MCP clients ignore this; for them the session allow is the
   check.

Every hardware action is logged (Settings → Agents → Recent agent actions). The capture limits still hold
(device voltage limit, voltage set before power-on). This guards the MCP tools only: the web UI and its API
are open to anyone on your network, so an agent with shell and network access could call them directly.
Firmware uploads by an agent go through PlatformIO and its token (see below); to keep auto mode from running
them, add an ask rule such as `"ask": ["Bash(pio run *upload*)"]` to the firmware project's
`.claude/settings.json`.

## Flashing from PlatformIO

With the sensor's own USB port connected (5 V blocked, see [Requirements](#requirements)), `pio run -t upload`
on any machine on the network sends the build to the tracker, which flashes it with esptool. The PPK2
keeps powering the sensor the whole time.

1. Put `platformio/tracker_upload.py` in the firmware project, next to `platformio.ini` (or download it from
   Settings → Firmware upload), and add an environment:
   ```ini
   [env:xiao_c6_tracker]
   extends = env:seeed_xiao_esp32c6
   upload_protocol = custom
   extra_scripts = post:tracker_upload.py
   custom_tracker_url = http://<tracker>:8080
   ; custom_tracker_on_recording = ask    ; ask | keep | stop | after | cancel
   ; custom_tracker_when_asleep = wait    ; wait | always | cycle (default: Settings → Firmware upload)
   ```
2. The upload token: **Settings → Firmware upload** shows it (and the `platformio.ini` lines with this
   tracker's address). Put it in the environment variable `PST_FLASH_TOKEN` on the machine you upload from.
3. `pio run -e xiao_c6_tracker -t upload`

What happens:

- **Sensor asleep** (its USB port disappears in deep sleep): by default the upload waits for its next wake
  (its sleep cycle stays as it is) and power-cycles it with the PPK2 if it hasn't woken after 90 s. Or it
  always waits (never power-cycles), or it power-cycles it right away. Set in Settings → Firmware upload, or per upload with
  `custom_tracker_when_asleep`. While it waits, the banner can power-cycle it now or cancel. Power-cycling
  needs the PPK2 voltage set by the tracker (switch the DUT on once).
- **Recording running:** the upload asks (in the terminal, or answer in the web UI): keep recording and mark
  the flash as a `flash` event, stop the recording first, flash when the recording stops, or cancel.
  `custom_tracker_on_recording` can answer up front. "After" queues it: the PlatformIO command ends right
  away and the tracker flashes when the recording stops. Resets during and just after a marked flash don't
  count as unexpected resets.
- An upload that's still waiting (for an answer, the recording or a wake) can be cancelled in the banner,
  and a newer upload replaces it. One that's flashing refuses new uploads until it's done.
- If esptool fails, the sensor is power-cycled and it's tried once more.

In the web UI: the **USB** chip in the top bar shows the port (asleep, closed, console open, flashing), a
banner asks about a running recording and shows progress, the serial log has a **UART | USB** switch for
the console (a firmware key menu becomes buttons), and a flash shows on the charts and under a session's
Other events, with a *Compare before / after* link.

The USB port stays **closed** otherwise, and the link is suspended: the firmware sees no USB host and
deep-sleeps as if nothing were plugged in. Opening the USB console (`POST /api/device/usb/open
{"on": true}`) catches the firmware's boot menu on the next boot (`menu` in `GET /api/device/usb`; answer
with `POST /api/device/usb/key {"key": "n"}`) and keeps the sensor awake while it's open. After closing
it, the firmware notices within a sleep retry or two (~30 s here) and sleeps again. The first boot after a
flash still sees USB (esptool's reset keeps the link up). The MCP server can't flash or use the USB console.

Measured on the XIAO ESP32-C6: the port appears ~0.4 s after power-on; a 1.8 MB flash takes ~18 s
including the power-cycle; reading the whole 4 MB flash (a backup) takes ~20 s.

## Operations

### Update

```
git pull
./dc pull
./dc up -d
```
(Built from source: `git pull` and `./dc up -d --build`.) To stay on a version, set `IMAGE_TAG=1.2` in
`.env`. The api creates the database schema on the first start and applies changes when it starts. Rebuilding `capture` during a recording
leaves a gap of about 4 seconds in it; to update one service only: `./dc up -d --build --no-deps web`.

### Backup

Two things hold your data: the **database** (Docker volume `inspector-watt_pgdata`) and the
**full-rate files** in `./data` (or `DATA_DIR`).

```
./dc exec -T db pg_dump -U tracker -Fc tracker > backup-$(date +%F).dump   # database, a few minutes at most
rsync -a data/ /path/to/backup/data/                                       # full-rate files
```
`pg_dump` warns about "circular foreign-key constraints" in TimescaleDB's own tables; that's expected.

### Move to another machine, or restore a backup

1. On the new machine: [Install](#install) steps 1–3. Copy `.env` (it holds settings and the first MCP
   token) and the `data/` folder over.
2. Start only the database (it starts empty) and restore into it:
   ```
   ./dc up -d db
   ./dc exec -T db psql -U tracker -d tracker -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
   ./dc exec -T db psql -U tracker -d tracker -c "SELECT timescaledb_pre_restore();"
   ./dc exec -T db pg_restore -U tracker -d tracker --clean --if-exists --no-owner < backup.dump
   ./dc exec -T db psql -U tracker -d tracker -c "SELECT timescaledb_post_restore();"
   ```
3. Start the rest: `./dc up -d` (or `./dc up -d --build` from source). Check the device paths
   (`ls -l /dev/ppk2 /dev/dut-uart`) and Settings → System health.

### Troubleshooting

| Problem | What to check |
|---|---|
| PPK2 or UART "not connected" | `ls -l /dev/ppk2 /dev/dut-uart`. Missing: replug, check the udev rule (and the adapter's ids). After a replug the stream resumes by itself. |
| "Permission denied" on the port | Headless machine: use `OWNER=` in the udev rule (see Install). User id not 1000: set `UID`/`GID` in `.env`. |
| Lost samples or gaps | Settings → System health. Plug the PPK2 straight into the computer (no hub, short cable) and keep heavy work off that machine. |
| DUT power shows "unknown" | Normal after the capture service restarted: it's known again after the next switch. The sensor keeps running. |
| Detail recording stops writing files | The disk reached `MIN_FREE_GB` free space. Free space or delete old sessions. |
| Containers restart for no reason (Bazzite) | Run compose through `./dc`, not `docker compose` from `/home/...`. |
| Upload says "flash token missing or wrong" | `PST_FLASH_TOKEN` must be the token from `/api/flash/settings`. |
| Flash fails: "USB port didn't appear" | `ls -l /dev/dut-usb` while the sensor is awake: install the udev rule again (it has a `dut-usb` line now), check the adapter's cable. |
| Firmware thinks USB is connected with the console closed | Something else opens the port: usually ModemManager, which the udev rule stops. |
| Agent on another machine gets "401" | Switch on Settings → Agents → *Other machines on the network* and use the token shown there. |

## Development

**Simulator stack:** simulated PPK2 and sensor, separate ports and data, can run next to the real one:
```
WEB_PORT=8090 WEB_BIND=127.0.0.1 ./dc -p pst-sim --env-file sim.env up -d --build
```
Open http://localhost:8090. Use it for UI tests, so the real board isn't switched by accident.

Frontend with hot reload against the running stack (http://localhost:5173):
```
docker run --rm -it --network host -v "$PWD/frontend":/app:z -w /app node:22-alpine npm run dev
```

Tests (GitHub Actions runs the same on every push and pull request, and publishes the images from `main`
and version tags):
```
docker build --target test -t pst-capture-test capture && docker run --rm pst-capture-test
docker build --target test -t pst-api-test api && docker run --rm pst-api-test
docker build --target test -t pst-mcp-test mcp-server && docker run --rm pst-mcp-test
(cd frontend && docker run --rm -v "$PWD":/app:z -w /app node:22-alpine npm run check)
```

## Reference

### How it fits together

```
capture (PPK2 + UART, recorder) ──► TimescaleDB (sessions, 10 ms/1 s/1 min/1 h summaries, UART lines, events)
        │                       └──► ./data/sessions/<id>/<kind>/*.parquet (full-rate data)
        └── RAM buffer (last 5 min) ◄── api (:8000) ◄── web (:8080, Svelte + uPlot)
                                          ▲
                                          └── mcp (:8300, agents; via web /mcp on the network)
```

The PPK2 sample index × 10 µs is the master clock; UART lines are mapped onto it (≈ 1–2 ms accuracy).

### Settings in `.env`

Defaults are in `compose.yaml`; see `.env.example`.

| Setting | Default | What |
|---|---|---|
| `PPK2_PORT`, `UART_PORT`, `USB_PORT` | `/host-dev/ppk2`, `/host-dev/dut-uart`, `/host-dev/dut-usb` | device paths (`sim://` = simulator) |
| `UART_BAUD` | 115200 | baud at start (a device can set its own) |
| `MAX_VOLTAGE_MV` | 4200 | hard limit for the PPK2 output |
| `IDLE_POWER_OFF_S` | 600 | default until changed in Settings → Measurement & safety (0 = never) |
| `AUTO_STREAM` | 1 | start measuring as soon as the PPK2 connects |
| `RING_SECONDS` | 300 | live buffer, about 0.5 MB RAM per second |
| `RAW_MANTISSA_BITS` | 12 | full-rate file precision; 23 = lossless |
| `MIN_FREE_GB` | 10 | stop writing full-rate files below this much free disk |
| `DATA_DIR` | `./data` | full-rate files |
| `WEB_PORT`, `WEB_BIND` | 8080, 127.0.0.1 | web UI; `0.0.0.0` = the whole network |
| `API_PORT`, `CAPTURE_PORT`, `MCP_PORT`, `DB_PORT` | 8000, 8100, 8300, 5432 | on 127.0.0.1 only |
| `MCP_TOKEN` | – | first start only; then Settings → Agents |
| `FLASH_TOKEN` | random | upload token, first start only; then `/api/flash/settings` |
| `IMAGE_TAG` | latest | published image version for `./dc pull` (`sim` in `sim.env`) |
| `DB_PASSWORD`, `LOG_LEVEL`, `UID`, `GID` | tracker, INFO, 1000, 1000 | |

### api (port 8000, on this machine; `/api/...` through the web port)

| | |
|---|---|
| `GET /api/sessions`, `GET /api/sessions/{id}` | list / details (duration, charge, average current, data size) |
| `PATCH`, `DELETE /api/sessions/{id}` | rename / notes, delete (database + files) |
| `POST /api/sessions`, `POST /api/sessions/stop` | start / stop recording (forwarded to capture) |
| `GET /api/sessions/{id}/trace?start_s=&end_s=&points=` | min/max/mean; source picked automatically (full-rate files, live buffer, or summaries) |
| `GET /api/sessions/{id}/stats?start_s=&end_s=` | mean current, charge, wake statistics |
| `GET /api/sessions/{id}/events?kind=` | wakes, gaps, restores, battery steps... |
| `GET /api/sessions/{id}/uart?start_s=&end_s=&q=` | UART lines, with text search |
| `GET /api/sessions/{id}/phases` | cycles and phases; `types[].instances` are ranges into all phases in order |
| `GET /api/sessions/{id}/cycles?type=&index=` | cycle summary per type, or one cycle |
| `GET /api/sessions/{id}/battery?capacity_mah=&sleep_s=&without=&type=&every=` | battery life with what-ifs |
| `POST /api/sessions/{id}/phases/test`, `.../types/test` | try rules / cycle types without saving |
| `/api/profiles`, `/api/presets`, `/api/devices`, `/api/devices/active` | phase profiles, record presets, devices |
| `/api/measurement`, `/api/storage`, `/api/mcp`, `/api/health` | the Settings pages |
| `/api/device/...` | the capture API below |

Example:
```
curl -XPOST localhost:8000/api/sessions -H 'content-type: application/json' \
  -d '{"name": "sleep test", "kind": "soak", "voltage_mv": 3700, "wake_threshold_ua": 500}'
curl -XPOST localhost:8000/api/sessions/stop -H 'content-type: application/json' -d '{}'
```
Battery emulation in the start request: `"battery": {"chemistry": "lipo", "cells": 1, "capacity_mah": 1000,
"speedup": 50, "cutoff_mv": 3000, "on_cutoff": "protect"}`, or `{"mode": "time", "start_mv": 4200,
"sweep_min": 120}`; `"hunt"` goes down to `floor_mv` (default 2500).

### Capture API (port 8100, internal; `/api/device/...` on the api)

| Method | Path | What |
|---|---|---|
| GET | `/status` | devices, stream stats (rate, lost samples, CPU load, clock drift), buffer |
| GET | `/ppk2/metadata` | calibration table read from the PPK2 |
| POST | `/ppk2/source` `{"voltage_mv": 3700}` | set voltage + source mode |
| POST | `/ppk2/dut` `{"on": true}` | sensor power (voltage must be set first) |
| PUT | `/ppk2/device` `{"id", "name", "max_mv"}` | the device on the bench and its voltage limit (set by the api) |
| POST | `/ppk2/stream` `{"on": true}` | start/stop measuring |
| POST | `/ppk2/spike-filter` `{"on": true}` | filter for range-switch spikes |
| GET, PUT | `/settings` | idle power-off, spike filter |
| GET | `/buffer/window?start=&end=&points=`, `/buffer/stats` | live buffer: raw, or min/max/mean bins |
| GET | `/uart/lines?after_seq=` | recent UART lines with PPK2 sample index |
| PUT | `/uart/config` `{"baud": 115200}` | change baud rate |
| POST | `/uart/send` `{"text": "..."}` | send to the sensor (TX not wired yet) |
| GET | `/usb` | the sensor's own USB port: present (awake), open, boot menu |
| POST | `/usb/open` `{"on": true}` | open/close the USB console (open = the firmware sees USB: no deep sleep) |
| GET | `/usb/lines?after_seq=` | USB console output (not saved in sessions: it repeats the UART log) |
| POST | `/usb/send` `{"text": "..."}`, `/usb/key` `{"key": "n"}` | send a line, or one key for a menu |
| POST | `/flash` | flash images with esptool (only through the api's `/api/flash`, which checks the token) |
| GET | `/flash`, `/flash/{id}?after=`, `/flash/jobs` | the last or a given flash job (log from line `after`), or the last few without logs |
| POST | `/flash/{id}/answer` `{"choice": "keep"}` | keep / stop / after / cancel when a recording is running |
| POST | `/flash/{id}/cancel`, `/flash/{id}/now` | cancel a waiting upload; stop waiting for a wake and power-cycle now |
| GET | `/battery/chemistries` | battery emulation curves |
| WS | `/live?bin_us=1000` | about 25 updates/s: bins, UART lines, status |
| POST | `/sessions`, `/sessions/stop`, `/sessions/save-buffer`; GET `/sessions/current` | recording |

### Data

- Database: sessions, summaries (10 ms, 1 s, 1 min, 1 h), UART lines, events, profiles, devices, settings.
  From this machine: `psql postgresql://tracker:tracker@localhost:5432/tracker`.
- Full-rate files: `data/sessions/<id>/<kind>/*.parquet`. Ad-hoc analysis with DuckDB:
  ```sql
  SELECT avg(current_a) FROM 'data/sessions/1/detail/*.parquet' WHERE idx BETWEEN 0 AND 100000;
  ```

### Hardware notes

- The PPK2 (source mode) powers a **Seeed XIAO ESP32-C6 through its battery pins**: it stands in for a
  1-cell LiPo, so `MAX_VOLTAGE_MV` defaults to 4200. Don't plug USB into the XIAO while measuring, unless
  it's through an adapter that blocks the 5 V.
- The XIAO's USB (USB-Serial/JTAG, 303a:1001) shows up about 1 s after power-on and disappears in deep
  sleep. The console opens it with DTR and RTS as the kernel sets them (both on): clearing them one after
  the other passes through "RTS on, DTR off", which resets the chip.
- The sensor never powers up at a voltage this service hasn't set. After a PPK2 replug outside a
  session, power isn't restored (set the voltage again first); inside a session the session's voltage is
  used.
- The PPK2 port is opened with DTR kept high on close (HUPCL off): dropping DTR after the port had been
  open a while sometimes switched the PPK2 output off, rebooting the sensor on every restart.
- The capture container mounts the host's `/dev` read-only and may open USB serial devices (major 166)
  only, so `/dev/ppk2` keeps working after a replug without restarting the container.

## License

[MIT](LICENSE).

Inspector Watt is an independent project, not affiliated with or endorsed by Nordic Semiconductor. "Power
Profiler Kit" and "PPK2" are Nordic Semiconductor's names for their product.
