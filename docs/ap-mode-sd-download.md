# HiveHub AP Mode, Button Handling, and SD Card Download

This document describes how to enter HiveHub AP/setup mode, how the setup button behaves during normal operation and deep sleep, how to perform a factory reset, and how to use the AP-mode web interface to download all SD card data.

## Firmware behavior overview

The firmware supports a setup/provisioning access point mode, also called AP mode. AP mode is used to configure WiFi/backend settings and, with the new SD download feature, download all files stored on the SD card.

Relevant button definitions in `firmware/include/config.h`:

```cpp
// External button. Wire button between this pin and GND. Uses INPUT_PULLUP.
// Short press: start WiFi provisioning AP.
// Long press: reset Preferences and reboot.
#define SETUP_BUTTON_PIN 27
static const unsigned long BUTTON_DEBOUNCE_MS = 50;
static const unsigned long BUTTON_LONG_PRESS_MS = 10000;
```

The button, AP/provisioning, and SD-download handlers live in
`firmware/src/portal.cpp`; deep-sleep entry and the EXT0 button-wake
configuration live in `firmware/src/storage_power.cpp`; the boot-time AP-entry
check is in `firmware/src/main.cpp`.

The setup button is connected to GPIO27 and should pull the pin to GND when pressed. The pin uses `INPUT_PULLUP`, so the button is considered pressed when the input reads `LOW`.

> **On the XIAO ESP32-C6 the setup button moved in firmware 0.25.0.** It is now
> the board's **on-board USER/BOOT button (GPIO9)**, and the external D2 button
> it used to occupy is the [inspection button](inspection-mode.md). Everything
> below still describes the behaviour exactly — short press opens the AP, long
> press factory-resets — only the button you press changed. The 30-pin ESP32
> DevKit is unaffected and keeps GPIO27.
>
> Two consequences worth knowing:
>
> * A hub sealed in an enclosure no longer has the setup button brought out. Use
>   the `start_provisioning` command (dashboard or API) to open the portal
>   remotely instead of opening the box — that is exactly what it is for. If
>   you want a physical button anyway, enable the optional
>   [setup button on D0](inspection-mode.md#external-setup-button-on-d0-optional):
>   it wakes the hub from deep sleep and opens the AP straight away.
> * **The USER button cannot wake the C6 from deep sleep, and holding it at
>   power-up flashes the chip rather than opening the AP.** GPIO9 is not an
>   LP-IO pad (only GPIO0–GPIO7 are), so it can never be a deep-sleep wake
>   source, and it is the boot strapping pin, so holding it across a RESET or a
>   power-up enters the serial bootloader. Neither is fixable in firmware. What
>   0.25.4 does fix is everything around it: the invalid wake pin no longer
>   silently disarms the inspection button on D2 as well, and the firmware now
>   polls the USER button throughout each wake cycle, so **press and hold it and
>   the AP opens at the end of the next cycle**. See
>   [inspection-mode.md](inspection-mode.md#the-button) for the full picture.

## Entering AP mode

### When the device is awake

Press and release the setup button briefly.

Expected result:

1. The firmware detects a short press.
2. AP mode starts.
3. The device creates a WiFi network named similar to:

```text
HiveHub-Setup-ABCD
```

4. Connect to that WiFi network.
5. Open the setup page in a browser:

```text
http://192.168.4.1
```

The exact AP SSID and IP address are also printed to the serial monitor when AP mode starts.

### When the device is in deep sleep

During deep sleep, the normal firmware loop is not running. That means a normal short press cannot be handled in the same way as when the ESP32 is awake.

**30-pin ESP32 DevKit (GPIO27).** The firmware configures GPIO27 as an EXT0 wake
source:

```cpp
esp_sleep_enable_ext0_wakeup((gpio_num_t)SETUP_BUTTON_PIN, 0);
```

This allows the setup button to wake the ESP32 from deep sleep when the button pulls GPIO27 LOW.

Recommended method:

1. Unplug power from the device.
2. Press and hold the setup button.
3. Plug power back in while still holding the button.
4. Keep holding the button for about 1-2 seconds.
5. Release the button.
6. Connect to the `HiveHub-Setup-XXXX` WiFi network.
7. Open:

```text
http://192.168.4.1
```

This method is more reliable than a very quick press during deep sleep, because the button is already held when the ESP32 boots and checks the setup button state.

**XIAO ESP32-C6 (GPIO9): the button does not wake it, and the power-up trick
flashes it instead.** GPIO9 has no LP-IO pad, so it cannot be armed as a
deep-sleep wake source at all, and it is the boot strapping pin, so holding it
across a power-up drops the chip into the serial bootloader. The steps above do
not apply to this board — do not use them.

What works on the C6:

1. **Press and hold** the USER button (do **not** cycle power).
2. Wait for the hub's next scheduled wake — up to one send interval, so **up to
   10 minutes** on the default cadence. The status LED blinks when it boots.
3. The AP comes up about a second later. The firmware reads the button at the
   very top of `setup()`, before anything else, so a button held across the wake
   opens the portal immediately and **skips that cycle's measurement** — the
   same thing a press on the 30-pin board's setup button does.

Holding it longer is safe: a hold that has already opened the portal is spent,
so it cannot roll on into the ten-second factory reset — that needs a release
and a fresh press.

> **Hold it, don't tap it.** The button is also polled four times inside each
> upload cycle (after the network connect, after the measurement, after the
> upload, and at the end), and a press caught there opens the AP when that cycle
> finishes. But those polls sit seconds apart around the cycle's blocking
> stages, so a quick tap between two of them is simply not seen. Holding is what
> makes the button deterministic — the boot-time read above cannot miss it.

#### Don't want to wait for the next wake? Use RESET first

The wait above exists only because the hub is asleep. If you can reach the
board, press **RESET** to make it wake right now:

1. Press and release **RESET**. Do **not** touch BOOT yet.
2. Now press and **hold BOOT** (the USER button, GPIO9), and keep holding.
3. If you get there within roughly a second and a half, the boot-time read
   catches it and the AP opens straight away with the measurement cycle
   skipped. If you are slower, the in-cycle polls catch it instead and the AP
   opens when that cycle finishes — **under a minute either way**.

> **The order matters, and getting it wrong flashes the chip.** BOOT is GPIO9,
> the boot-mode strapping pin, so it must be released *while* RESET is
> released. Holding BOOT down across the RESET — the usual "hold BOOT, tap
> RESET" flashing gesture — puts the chip into the serial bootloader and no
> firmware runs at all. Press RESET first, let go of it, *then* press BOOT.

Both buttons are on the board itself, so this route needs the enclosure open.
For a sealed hub use **Start AP mode** in the dashboard (below) instead.

Or, better, skip the button entirely and use **Start AP mode** in the dashboard
(below) — no waiting, and no reason to open the enclosure.

### Without touching the device

**Device & admin → Configuration → Start AP mode** in the built-in dashboard
(`POST /api/v1/local/devices/{device_id}/provisioning/start`, admin only) queues
a `start_provisioning` command — the same command the API exposes for HivePal
and for `curl` (see [api.md](api.md)).

It is queued, not immediate: the hub is asleep between cycles, so it picks the
command up on its next check-in and opens the AP **at the end of that cycle**,
once the readings are uploaded and the OTA check is done — the same point in the
boot sequence a button press reaches. Expect it up to one send interval after
the button is clicked.

From there it behaves exactly like a button press: the `HiveHub-Setup-XXXX`
network appears, the device is off WiFi and sending nothing while the portal is
open, and the portal closes itself on the provisioning timeout if nobody
connects. So only start it when somebody is at the hive to use it.

**The hub reboots first when it has to.** A BLE scan may only run in the NimBLE
port lifetime that owns the scanner (see `include/ble_stack.h`), and a hub with
a sensor already paired has spent that lifetime on the measurement scan by the
time the cycle ends. Opening the portal in place would therefore give it a
discovery scan that never runs — the AP comes up, the radio is fine, and the
pairing dropdowns say "No BLE devices found" next to a sensor advertising a
metre away. That was the 0.25.3 bug, and 0.25.4 shipped the reboot with a marker that
could not survive it (`RTC_DATA_ATTR` is restored from the app image by the
bootloader on any reboot that is not a deep-sleep wake). From 0.25.5 the hub
parks the request in
RTC memory and reboots, so the portal opens **before** the first measurement
cycle, exactly where a button press opens it, and the scan works. You will see
one extra reboot in the serial log and the AP appears a few seconds later than
it used to; nothing else changes, and the command has already been acknowledged
to the server before the reboot.

## Important: long hold / factory reset behavior

A long hold of the setup button performs a factory reset of Preferences and reboots the device.

Current behavior:

- Short press: start AP/setup mode.
- Long press for 10 seconds: factory reset Preferences and reboot.

Factory reset is triggered by this logic in `handleButton()`:

```cpp
if (down && buttonWasDown && !longPressHandled && now - buttonDownMs >= BUTTON_LONG_PRESS_MS) {
  longPressHandled = true;
  Serial.println("[BUTTON] Long press detected: factory reset Preferences");
  factoryResetPreferences();
}
```

## AP-mode SD card download feature

The AP-mode web interface  includes a button for downloading all data from the SD card.

On the setup page, a new section is shown:

```text
SD card data
[Download all SD data (.tar)]
```

Clicking this button downloads the SD card contents as a TAR archive:

```text
hivescale-sd-data.tar
```

The firmware endpoint is:

```text
GET /sd/download-all
```

The route is registered in `startProvisioningPortal()`:

```cpp
setupServer.on("/sd/download-all", HTTP_GET, handleSdDownloadAll);
```

The button is added to the AP-mode HTML page:

```cpp
html += "<p><a class='button' href='/sd/download-all'>Download all SD data (.tar)</a></p>";
```

## Why TAR instead of ZIP?

The download uses TAR instead of ZIP because TAR can be streamed directly from the ESP32 with very little RAM usage.

This is important because the ESP32 should not try to load the full SD card contents into memory before sending the download. The firmware walks the SD card directory tree and streams each file into the TAR response.

## Extracting the downloaded data

### macOS / Linux

```bash
tar -xf hivescale-sd-data.tar
```

### Windows PowerShell

```powershell
tar -xf hivescale-sd-data.tar
```

Modern Windows includes `tar` by default. If that is not available, 7-Zip can also open `.tar` files.

## After downloading: import the readings into HivePal

Downloading the SD data is only half of the round-trip. The archive exists so a
beekeeper can recover the measurements that were buffered on the card — for
example readings taken while the device was offline, off-grid, or unable to
reach the backend — and load them into the HiveHub database without losing
the historical record.

The card holds two append-only NDJSON files (one JSON object per line):

| File | Contents |
|---|---|
| `measurements.ndjson` | Every reading the device has taken, written at each wake-up. This is the permanent backup. |
| `cache.ndjson` | The retry queue of readings that have not yet been confirmed as uploaded. |

To get those readings into HiveHub, upload the file through HivePal:

1. Download `hivescale-sd-data.tar` from the device in AP mode (above).
2. Open **HiveHub** in HivePal and select the claimed device.
3. Use the **Import SD card data** card to upload either the whole
   `hivescale-sd-data.tar` or an extracted `measurements.ndjson`.
4. HivePal parses the file and forwards the readings to the HiveHub backend's
   bulk-import endpoint.

The import goes to:

```text
POST /api/v1/app/devices/{device_id}/measurements/import
```

See [`api.md`](api.md#post-apiv1appdevicesdevice_idmeasurementsimport) for the
endpoint contract and the HivePal repository's
`apps/hivescale/hivescale-integration.md` for the proxy and UI details.

The import is **idempotent**: `(device_id, measured_at)` is treated as the
natural key, so re-uploading the same file — or a file that overlaps with
readings the device already sent over the network — inserts only the genuinely
new rows and skips the rest. There is no harm in uploading the full backup every
time.

The built-in dashboard has the same **Import SD card data** upload on its
**Device & admin** page (`POST /api/v1/local/devices/{device_id}/measurements/import`),
so a self-host does not need HivePal for this. It also reads that upload in the
other direction: **Download / backup data** writes the server's stored readings
back out in this same NDJSON format, which is what makes a database re-deploy or
a move to another server recoverable — see
[backup-restore.md](backup-restore.md).

## Operational notes

- The SD download button is only shown when the SD card is available.
- Large SD cards or slow connections may take a while to download.
- Keep the browser open until the download completes.
- The firmware streams the TAR file directly, so RAM usage stays low.
- Very long file paths may be skipped because the simple TAR header implementation only supports names up to 99 characters.
- AP mode currently times out after the configured provisioning timeout if no reset/save action keeps the device active.

## Related code locations

| Purpose | File | Code reference |
|---|---|---|
| Setup button pin | `include/config.h` | `SETUP_BUTTON_PIN` |
| Factory reset hold duration | `include/config.h` | `BUTTON_LONG_PRESS_MS` |
| Deep-sleep wake from button | `src/storage_power.cpp` | `configureButtonWake()` |
| Boot-time AP entry check | `src/main.cpp` | `digitalRead(SETUP_BUTTON_PIN) == LOW \|\| wakeReason == ESP_SLEEP_WAKEUP_EXT0` |
| Button short/long press handling | `src/portal.cpp` | `handleButton()` |
| Remote AP entry (`start_provisioning`) | `src/hivehub_network.cpp` | `requestProvisioningPortal()` in `checkCommands()` |
| Deferred AP start at the end of a cycle | `src/main.cpp`, `src/portal.cpp` | `startRequestedProvisioningPortal()` |
| Reboot so the portal's BLE scan can run | `src/portal.cpp`, `src/main.cpp` | `rtcPortalBootMagic`, `consumePortalBootRequest()` |
| Setup-button poll during a wake cycle | `src/portal.cpp`, `src/main.cpp` | `pollSetupButton()` |
| "Can a discovery scan run right now?" | `src/ble_sensor.cpp`, `src/ble_stack.cpp` | `blesensor::discoveryAvailable()`, `blestack::scanWouldBeAllowed()` |
| SD TAR streaming helpers | `src/portal.cpp` | `tarSafeName()`, `writeTarHeader()`, `streamTarDirectory()` |
| SD download HTTP handler | `src/portal.cpp` | `handleSdDownloadAll()` |
| AP-mode download button | `src/portal.cpp` | `handleSetupRoot()` |
| SD download route | `src/portal.cpp` | `setupServer.on("/sd/download-all", HTTP_GET, handleSdDownloadAll)` |
| Factory reset of Preferences | `src/device_prefs.cpp` | `factoryResetPreferences()` |

## Recommended user instructions

To enter setup mode reliably:

1. Unplug the device.
2. Hold the setup button.
3. Plug the device back in.
4. Release the button after 1-2 seconds.
5. Connect to the `HiveHub-Setup-XXXX` WiFi network.
6. Open `http://192.168.4.1`.

To factory reset:

1. Make sure the device is powered and awake.
2. Hold the setup button for the configured long-press duration.
3. Release after the device logs or performs the reset.

Default factory-reset hold time: 10 seconds.
