# Logging for Inspector Watt

Inspector Watt splits every wake-up of your sensor into **phases** (boot, measure, connect, upload, sleep)
by reading its serial log, and shows the time, current and charge of each. The **Default** profile does this
without any setup, as long as the firmware prints a few plain lines. This page lists them.

It's written for people and for coding agents alike: hand it to your agent with "add Inspector Watt
logging to this firmware" (see [A prompt for your coding agent](#a-prompt-for-your-coding-agent)).

## The lines

Print each line **when the phase starts**, in this order, and leave out the phases your firmware doesn't
have. Case doesn't matter, and anything may come before it on the line (your logger's prefix, a timestamp,
a tag), so `[I][main.cpp:42] Measuring` and `I (1234) app: measuring 3 sensors` both work. Text after the
key word is free.

| Phase | Print at its start | Matches |
|---|---|---|
| **Boot** (starts a new cycle) | ESP32: nothing, the chip prints `rst:0x…` itself. Other chips: `Booting` | `rst:0x`, the word `booting` |
| **Measure** | `Measuring` | the word `measuring` |
| **Connect** | `Connecting to WiFi` (or LTE, LoRa, BLE…) | the word `connecting` |
| **Upload** | `Uploading 10 readings` | `uploading`, `sending` or `publishing` |
| **Sleep** | `Going to deep sleep for 60 s` | `going to sleep`, `entering deep sleep`, `entering light sleep`… |

And once per wake, anywhere in it:

| What | Print | Used for |
|---|---|---|
| **Wake reason** | `Wake source: Timer` (or `Button`, `Motion`, `Cold boot`…) | cycle types by wake reason, "woken by" counts |
| **Restart reason** | `Rebooting: firmware update` right before a deliberate restart | explains the reset in the list of unexpected resets (ESP32) |

Zephyr prints `*** Booting Zephyr OS build … ***` on every boot, so on nRF boards Boot works by itself.

### Rules of thumb

- **Print at the start** of the work, not after it: the phase begins at the line.
- **Flush before sleeping.** The sleep line is the last thing the chip says; if it's still in the UART
  buffer when the chip powers down, it's lost and the sleep phase is missing. Arduino: `Serial.flush()`.
  ESP-IDF: `uart_wait_tx_idle_polling()`. Zephyr: use immediate logging (`CONFIG_LOG_MODE_IMMEDIATE=y`) or
  `LOG_PANIC()` before sleeping.
- **Keep the order.** A phase is only looked for after the previous one was found. If your firmware connects
  before it measures, make your own profile (below) with the phases in your order.
- **ESP32: don't print `Booting`.** The chip's `rst:0x…` line already starts the cycle; a second start line
  would split each wake in two.
- **Don't use the key words elsewhere.** A debug line like `sending DHCP request` during Connect would start
  Upload early. `Rebooting` is fine (it isn't the word `booting`).
- **Timestamps aren't needed.** The tracker timestamps every line itself (they arrive 1–2 ms after the
  work starts; the Sleep boundary is moved to where the current actually drops).
- The UART runs at **115200 baud** unless you set another rate for the device in Settings → Devices.

## Example

What the tracker sees from an ESP32 that measures every wake and uploads every few wakes:

```
rst:0x5 (SLEEP_WAKEUP),boot:0x7e (SPI_FAST_FLASH_BOOT)
[I][main.cpp:21] Wake source: Timer
[I][main.cpp:30] Measuring
[I][main.cpp:52] Going to deep sleep for 60 s
rst:0x5 (SLEEP_WAKEUP),boot:0x7e (SPI_FAST_FLASH_BOOT)
[I][main.cpp:21] Wake source: Timer
[I][main.cpp:30] Measuring
[I][net.cpp:12] Connecting to WiFi
[I][net.cpp:40] Uploading 5 readings
[I][main.cpp:52] Going to deep sleep for 60 s
```

Two cycles: the first is a *Measure only* wake (Boot, Measure, Sleep), the second a *With upload* wake (Boot,
Measure, Connect, Upload, Sleep).

## Code

Arduino (ESP32):

```cpp
void setup() {
  Serial.begin(115200);
  Serial.printf("Wake source: %s\n", wakeReason());  // Timer, Button, Cold boot...

  Serial.println("Measuring");
  readSensors();

  if (shouldUpload()) {
    Serial.println("Connecting to WiFi");
    connectWifi();
    Serial.println("Uploading readings");
    upload();
  }

  Serial.printf("Going to deep sleep for %d s\n", SLEEP_S);
  Serial.flush();  // or the line is lost when the chip sleeps
  esp_sleep_enable_timer_wakeup(SLEEP_S * 1000000ULL);
  esp_deep_sleep_start();
}

const char *wakeReason() {
  switch (esp_sleep_get_wakeup_cause()) {
    case ESP_SLEEP_WAKEUP_TIMER: return "Timer";
    case ESP_SLEEP_WAKEUP_EXT0:
    case ESP_SLEEP_WAKEUP_EXT1:
    case ESP_SLEEP_WAKEUP_GPIO: return "Button";
    case ESP_SLEEP_WAKEUP_UNDEFINED: return "Cold boot";
    default: return "Other";
  }
}
```

ESP-IDF:

```c
ESP_LOGI(TAG, "Wake source: %s", wake_reason());
ESP_LOGI(TAG, "Measuring");
/* ... */
ESP_LOGI(TAG, "Going to deep sleep for %d s", SLEEP_S);
uart_wait_tx_idle_polling(CONFIG_ESP_CONSOLE_UART_NUM);
esp_deep_sleep(SLEEP_S * 1000000ULL);
```

Zephyr (nRF and others):

```c
LOG_INF("Wake source: %s", wake_reason());   /* Boot comes from "*** Booting Zephyr OS ***" */
LOG_INF("Measuring");
/* ... */
LOG_INF("Connecting to LTE");
/* ... */
LOG_INF("Uploading %d readings", n);
/* ... */
LOG_INF("Going to sleep for %d s", SLEEP_S);
```

## A key menu on the USB console

If the sensor's own USB port is connected (see the README), the web UI's USB console turns a key menu into
buttons. Print a title between dashes, one `[key] label` line per option, and then any other line (the
prompt); a line of dashes in between is fine:

```
--- DEBUG MENU ---
  [n]  Continue normally
  [r]  Reboot
------------------
Waiting for input...
```

A key is 1 to 8 characters without spaces; pressing the button sends it, without a newline.

## Your own phases

The Default profile is a starting point. For other phases (or another order) make your own profile: **Phase
rules** on a session page, or Settings → Phase profiles. Each rule is a piece of text or a regular expression;
the first rule starts a cycle. A device (Settings → Devices) picks the profile its new sessions get.

Agents can do this too: the MCP tools `test_phase_rules` and `test_cycle_types` try rules on a recorded
session, and `save_profile` stores them.

## A prompt for your coding agent

> Add Inspector Watt logging to this firmware, following docs/firmware-logging.md from
> https://github.com/mcpijnappel/inspector-watt: print `Wake source: <reason>` once per wake, `Measuring`,
> `Connecting to …`, `Uploading …` and `Going to deep sleep for <n> s` at the start of those phases (only the
> ones this firmware has, in that order), `Booting` first thing on non-ESP32 chips without Zephyr, a
> `Rebooting: <why>` line before deliberate restarts, and flush the UART before sleeping. Don't use those key
> words in other log lines. Keep the existing log output otherwise.
