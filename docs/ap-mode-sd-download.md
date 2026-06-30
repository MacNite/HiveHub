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

However, the firmware configures GPIO27 as an EXT0 wake source:

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
